import os
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from backend.catchup import CatchUp
from backend.manager import Manager
from backend.remote import RemoteLocations
from backend.recap_schedule import RecapSchedule


class Worker:
    def __init__(self):
        self.prepares = []; self.generates = []; self.job = None; self.sources = [{'sourceId':'C1'}]
    def providers(self): return [{'id':'codex','available':True}]
    def prepare(self, *args):
        self.prepares.append(args); self.job = {'status':'complete'}
    def status(self): return {'job':self.job, 'preview':{'id':'preview', 'sources':self.sources}}
    def generate(self, *args):
        self.generates.append(args); self.job = {'status':'complete','result':{'summaryId':'a'*32}}
    def cancel(self): self.job = {'status':'canceled', 'error':'Canceled'}
    def close(self): pass


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.manual = CatchUp(RemoteLocations(Manager(self.root/'home', self.root/'data')), threading.RLock())
        self.schedule = RecapSchedule(self.manual); self.worker = Worker(); self.schedule.worker = self.worker
        self.morning = datetime.fromisoformat('2026-09-27T09:01:00+00:00')
    def tearDown(self): self.schedule.close(); self.manual.close(); self.temp.cleanup()
    def enable(self): self.schedule.configure(True)
    def test_disabled_by_default_and_waits_until_time(self):
        self.schedule.tick(self.morning); self.assertEqual(self.worker.prepares, [])
        self.enable(); self.schedule.tick(self.morning.replace(hour=8)); self.assertEqual(self.worker.prepares, [])
    def test_one_combined_recap_per_day_and_restart_does_not_duplicate(self):
        self.enable()
        for _ in range(5): self.schedule.tick(self.morning)
        self.assertEqual(len(self.worker.prepares),1); self.assertEqual(len(self.worker.generates),1)
        self.assertEqual(self.worker.generates[0][1], ['C1']); self.assertEqual(self.schedule.status()['summaryId'],'a'*32)
        restarted = RecapSchedule(self.manual); restarted.worker = Worker(); restarted.tick(self.morning)
        self.assertEqual(restarted.worker.prepares, []); restarted.close()
        self.schedule.tick(self.morning.replace(day=28)); self.assertEqual(len(self.worker.prepares),2)
    def test_busy_manual_operation_defers_daily_run(self):
        self.enable(); self.manual.job = {'status':'running'}; self.schedule.tick(self.morning)
        self.assertEqual(self.worker.prepares, []); self.manual.job = None
        self.schedule.tick(self.morning); self.assertEqual(len(self.worker.prepares),1)
    def test_empty_day_does_not_call_provider(self):
        self.worker.sources = []; self.enable()
        self.schedule.tick(self.morning); self.schedule.tick(self.morning)
        self.assertEqual(self.worker.generates, []); self.assertEqual(self.schedule.status()['lastStatus'], 'empty')
    def test_disable_between_preview_and_generation_prevents_sending(self):
        self.enable(); self.schedule.tick(self.morning); self.schedule.configure(False); self.schedule.tick(self.morning)
        self.assertEqual(self.worker.generates, []); self.assertEqual(self.schedule.status()['lastStatus'],'canceled')
    def test_failed_day_is_not_retried_or_backfilled(self):
        self.enable(); self.schedule.tick(self.morning); self.worker.job = {'status':'error','error':'fixture'}
        self.schedule.tick(self.morning); self.schedule.tick(self.morning)
        self.assertEqual(len(self.worker.prepares),1)
        self.schedule.tick(self.morning.replace(day=30)); self.assertEqual(len(self.worker.prepares),2)
        self.assertEqual(datetime.fromisoformat(self.worker.prepares[-1][0]).day,29)
    @unittest.skipUnless(hasattr(time,'tzset'), 'Needs POSIX timezone switching')
    def test_daily_windows_handle_daylight_saving(self):
        try:
            with patch.dict(os.environ, {'TZ':'America/Chicago'}):
                time.tzset()
                self.enable()
                self.schedule.tick(datetime.fromisoformat('2026-03-09T09:01:00-05:00'))
                start,end = map(datetime.fromisoformat,self.worker.prepares[0][:2])
                self.assertEqual((end-start).total_seconds(),23*3600)
        finally:
            time.tzset()
    def test_invalid_time_and_missing_provider_rejected(self):
        for value in ('25:00','9:00','no'):
            with self.assertRaises(ValueError): self.schedule.configure(True,time=value)
        with self.assertRaises(ValueError): self.schedule.configure(True,provider='claude')

    def test_real_daily_pipeline_saves_into_manual_history(self):
        import json
        source = self.root / 'home/.codex/sessions/fixture.jsonl'; source.parent.mkdir(parents=True)
        source.write_text(json.dumps({'type':'session_meta','payload':{'id':'daily-fixture','cwd':str(self.root)}})+'\n'+json.dumps({'timestamp':'2026-09-26T16:00:00Z','type':'response_item','payload':{'type':'message','role':'user','content':[{'text':'Review the project'}]}})+'\n')
        self.schedule.worker = CatchUp(self.manual.remote, self.manual.manager_lock)
        def answer(provider, model, prompt):
            data = json.loads(prompt.split('BEGIN QUOTED EVIDENCE\n')[1].split('\nEND QUOTED EVIDENCE')[0])
            item = data['sources'][0]
            return {'overview':'Review planned.','projects':[{'projectId':item['projectId'],'summary':'Review requested.','decisions':[],'openLoops':['Review pending'],'nextSteps':[],'sourceIds':[item['sourceId']]}]}
        with patch.dict(os.environ, {'CODEX_HOME':str(self.root/'home/.codex')}), patch.object(self.schedule.worker,'providers',return_value=[{'id':'codex','available':True}]), patch.object(self.schedule.worker,'_run_provider',side_effect=answer):
            self.enable()
            for _ in range(200):
                self.schedule.tick(self.morning)
                if self.schedule.status()['lastStatus']=='complete': break
                time.sleep(.01)
            self.assertEqual(self.schedule.status()['lastStatus'],'complete')
        history = self.manual.history(); self.assertEqual(len(history),1)
        self.assertEqual(history[0]['id'],self.schedule.status()['summaryId'])
