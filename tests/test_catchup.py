import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.activity import activity_excerpt, time_window
from backend.catchup import CatchUp, generator_command, validate_summary
from backend.manager import Manager
from backend.remote import RemoteLocations


class CatchUpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.home = self.root/'home'; self.home.mkdir()
        self.env = patch.dict(os.environ, {'HOME': str(self.home)}); self.env.start()
        self.codex_env = os.environ.pop('CODEX_HOME', None)
        self.manager = Manager(self.home, self.root/'data')
        self.source = self.home/'.codex/sessions/2026/09/25/session.jsonl'; self.source.parent.mkdir(parents=True)
        self.write([
            ('2026-09-25T04:59:59Z', 'Previous local day'),
            ('2026-09-25T05:00:00Z', 'Start of yesterday in Chicago'),
            ('2026-09-26T04:59:59Z', 'End of yesterday in Chicago'),
            ('2026-09-26T05:00:00Z', 'Today, excluded'),
            ('', 'Unknown date, excluded'),
        ])
        self.manager.scan(); self.remote = RemoteLocations(self.manager); self.service = CatchUp(self.remote, threading.RLock())
        self.start, self.end = '2026-09-25T00:00:00-05:00', '2026-09-26T00:00:00-05:00'

    def tearDown(self):
        self.service.close(); self.env.stop()
        if self.codex_env is not None: os.environ['CODEX_HOME'] = self.codex_env
        self.temp.cleanup()

    def write(self, messages):
        rows = [{'type': 'session_meta', 'payload': {'id': 'catch-up-fixture', 'cwd': str(self.home/'project')}}]
        rows += [{'timestamp': stamp, 'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [{'text': content}]}} for stamp, content in messages]
        self.source.write_text(''.join(json.dumps(row)+'\n' for row in rows))

    def finish(self):
        deadline = time.monotonic()+10
        while time.monotonic()<deadline:
            result = self.service.status()
            if result['job']['status'] != 'running': return result
            time.sleep(0.01)
        self.fail('Job did not complete')

    def prepare(self):
        self.service.prepare(self.start, self.end, 'Yesterday · America/Chicago')
        result = self.finish(); self.assertEqual(result['job']['status'], 'complete', result)
        return result['preview']

    def test_timezone_boundaries_not_file_modification_time(self):
        c = next(iter(self.manager.contexts.values()))
        os.utime(self.source, (1,1))
        result = activity_excerpt(c, self.start, self.end)
        self.assertEqual([m['content'] for m in result['messages']], ['Start of yesterday in Chicago', 'End of yesterday in Chicago'])
        self.assertEqual(result['undated'], 1)
        self.assertEqual(result['matched'], 2)

    def test_invalid_windows_rejected(self):
        for start,end in [('2026-01-01','2026-01-02'), (self.end,self.start), ('2020-01-01T00:00:00Z',self.end)]:
            with self.assertRaises(ValueError):time_window(start,end)

    def test_bounded_tail_reports_partial_coverage(self):
        self.write([('2026-09-25T12:00:00Z', 'first '+ 'x'*1000), ('2026-09-25T13:00:00Z', 'last')])
        c = next(iter(self.manager.contexts.values()))
        with patch('backend.activity.ACTIVITY_BYTES', 400): result = activity_excerpt(c,self.start,self.end)
        self.assertTrue(result['partial']); self.assertEqual(result['messages'][-1]['content'],'last'); self.assertLessEqual(result['bytesRead'],400)

    def test_preview_is_local_and_deduplicates_imported_history(self):
        archive = self.root/'copy.gptctx'; self.manager.export(list(self.manager.contexts),archive); self.manager.import_archive(archive)
        with patch('backend.catchup.subprocess.Popen') as popen:
            preview=self.prepare(); popen.assert_not_called()
        self.assertEqual(len(preview['sources']),1); self.assertEqual(preview['duplicates'],1)
        self.assertTrue(preview['warnings'])
        self.assertEqual(self.service.history(),[])

    def test_unselected_or_fabricated_sources_rejected(self):
        preview=self.prepare(); source=preview['sources'][0]
        value={'overview':'Your recap','projects':[{'projectId':source['projectId'],'summary':'Work','decisions':[],'openLoops':[],'nextSteps':[],'sourceIds':['invented']} ]}
        with self.assertRaisesRegex(ValueError,'outside'):validate_summary(value,preview['sources'])
        value['projects'][0]['sourceIds']=[source['sourceId']]
        self.assertEqual(validate_summary(value,preview['sources'])['overview'],'Your recap')

    def fake_cli(self, sleeping=False):
        binary=self.root/'bin'; binary.mkdir(exist_ok=True)
        executable=binary/'codex'
        executable.write_text('#!'+sys.executable+'\nimport sys,json,time\nfrom pathlib import Path\n'+('time.sleep(20)\n' if sleeping else '')+'''text=sys.stdin.read()
data=json.loads(text.split('BEGIN QUOTED EVIDENCE\\n',1)[1].split('\\nEND QUOTED EVIDENCE',1)[0])
groups={}
for source in data['sources']: groups.setdefault(source['projectId'],[]).append(source['sourceId'])
value={'overview':'Fixture recap','projects':[{'projectId':key,'summary':'Reported activity from the selected excerpts.','decisions':[],'openLoops':['Review the next task'],'nextSteps':['Suggested: reopen the linked conversation'],'sourceIds':ids} for key,ids in groups.items()]}
Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(json.dumps(value))
''')
        executable.chmod(0o700)
        return patch.dict(os.environ,{'PATH':str(binary)+os.pathsep+os.environ.get('PATH','')})

    def test_cli_generation_save_reload_delete_keeps_original_unchanged(self):
        preview=self.prepare(); before=self.source.read_bytes()
        with self.fake_cli():
            self.service.generate(preview['id'],[s['sourceId'] for s in preview['sources']],'codex')
            result=self.finish()
        self.assertEqual(result['job']['status'],'complete',result)
        record=self.service.get(result['job']['result']['summaryId'])
        self.assertEqual(record['overview'],'Fixture recap'); self.assertEqual(self.source.read_bytes(),before)
        self.assertEqual(len(CatchUp(self.remote,threading.RLock()).history()),1)
        self.assertEqual(self.service.delete(record['id']),[])

    def test_cancel_terminates_cli_and_saves_no_recap(self):
        preview=self.prepare()
        with self.fake_cli(sleeping=True):
            self.service.generate(preview['id'],['C1'],'codex')
            deadline=time.monotonic()+5
            while self.service.process is None and time.monotonic()<deadline:time.sleep(.01)
            self.assertIsNotNone(self.service.process)
            self.service.cancel(); result=self.finish()
        self.assertEqual(result['job']['status'],'canceled'); self.assertEqual(self.service.history(),[])

    def test_commands_disable_persistence_and_do_not_resume_originals(self):
        codex=generator_command('codex','/fixture/codex',self.root)
        claude=generator_command('claude','/fixture/claude',self.root)
        self.assertIn('--ephemeral',codex); self.assertIn('read-only',codex); self.assertIn('--ignore-user-config',codex)
        self.assertIn('--no-session-persistence',claude); self.assertIn('--safe-mode',claude); self.assertEqual(claude[claude.index('--tools')+1],'')
        for args in (codex,claude):
            self.assertNotIn('--resume',args); self.assertNotIn('--dangerously-skip-permissions',args)
        with self.assertRaises(ValueError):generator_command('codex','/fixture/codex',self.root,'--dangerous')

    def test_prepare_preserves_sources_when_nothing_has_a_date(self):
        self.write([('', 'Undated')]); preview=self.prepare()
        self.assertEqual(preview['sources'],[]); self.assertIn('without a timezone',preview['warnings'][0])


if __name__=='__main__':unittest.main()
