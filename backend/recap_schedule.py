"""Opt-in daily recaps while the app is open; one attempt per local calendar day."""
import copy
import re
import threading
from datetime import datetime, timedelta, time
from pathlib import Path

try:
    from .catchup import CatchUp, generator_command
    from .manager import atomic_json, read_json
except ImportError:
    from catchup import CatchUp, generator_command
    from manager import atomic_json, read_json


class RecapSchedule:
    def __init__(self, manual):
        self.manual = manual
        self.worker = CatchUp(manual.remote, manual.manager_lock, manual.setup)
        self.path = manual.remote.manager.data / 'catch-up-schedule.json'
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.thread = None
        self.phase = None
        self.state = {'enabled': False, 'time': '09:00', 'provider': 'codex', 'model': '', 'include_remote': False,
                      'lastAttempt': None, 'lastStatus': None, 'message': '', 'summaryId': None}
        self.state.update(read_json(self.path, {}))
        if self.state['lastStatus'] == 'running':
            self.state.update(lastStatus='interrupted', message='The app closed during the daily recap. The next attempt will be on the next scheduled day; you can generate a manual recap now.')
            self.save()

    def save(self):
        atomic_json(self.path, self.state)
        self.path.chmod(0o600)

    def status(self):
        with self.lock:
            result = copy.deepcopy(self.state)
            if self.phase:
                job = self.worker.status()['job']
                result['message'] = job.get('message', '')
            return result

    def configure(self, enabled, time='09:00', provider='codex', model='', include_remote=False):
        if not isinstance(enabled, bool) or not isinstance(include_remote, bool) or not isinstance(time, str) or not re.fullmatch(r'([01][0-9]|2[0-3]):[0-5][0-9]', time):
            raise ValueError('Choose a valid daily time.')
        if provider not in ('codex', 'claude'): raise ValueError('Choose Codex or Claude.')
        generator_command(provider, provider, Path('/unused'), model)
        if enabled and not any(p['id'] == provider and p['available'] for p in self.worker.providers()):
            raise ValueError('Install and sign into the selected provider in AI setup first.')
        with self.lock:
            if self.phase and enabled: raise ValueError('Wait for the daily recap to finish, or turn the schedule off first.')
            self.state.update(enabled=enabled, time=time, provider=provider, model=model, include_remote=include_remote)
            self.save()
            if not enabled and self.phase: self.worker.cancel()
        return self.status()

    def tick(self, current=None):
        current = current or datetime.now().astimezone()
        with self.lock:
            if self.stop.is_set(): return
            if self.phase:
                status = self.worker.status(); job = status['job']
                if job['status'] == 'running': return
                if job['status'] != 'complete' or not self.state['enabled']:
                    self._finish('canceled' if not self.state['enabled'] else job['status'], job.get('error', 'Daily recap canceled.'))
                elif self.phase == 'preview':
                    preview = status['preview']
                    if not preview['sources']:
                        self._finish('empty', 'No dated activity was found for yesterday. No AI request was made.')
                    else:
                        try:
                            self.worker.generate(preview['id'], [s['sourceId'] for s in preview['sources']], self.state['provider'], self.state['model'])
                            self.phase = 'generate'
                        except Exception:
                            self._finish('error', 'Daily recap could not start. Check provider sign-in and use Catch up to retry manually.')
                else:
                    self.state['summaryId'] = job['result']['summaryId']
                    self._finish('complete', 'Daily recap saved. Open it in Saved recaps.')
                return
            if not self.state['enabled'] or current.strftime('%H:%M') < self.state['time']: return
            day = current.date().isoformat()
            if self.state['lastAttempt'] and self.state['lastAttempt'] >= day: return
            with self.manual.lock:
                if self.manual.job and self.manual.job['status'] == 'running': return
                # Convert each local midnight separately, preserving DST day length.
                end = datetime.combine(current.date(), time.min).astimezone()
                start = datetime.combine(current.date() - timedelta(days=1), time.min).astimezone()
                self.state.update(lastAttempt=day, lastStatus='running', message='Preparing yesterday’s daily recap…', summaryId=None)
                self.save()  # Reserve before work: restart/failure never silently re-bills today.
                try:
                    self.worker.prepare(start.isoformat(), end.isoformat(), 'Daily recap · ' + start.date().isoformat(), self.state['include_remote'], True)
                    self.phase = 'preview'
                except Exception:
                    self._finish('error', 'Daily recap could not read activity. Try a manual recap.')

    def _finish(self, status, message):
        self.phase = None
        self.state.update(lastStatus=status, message=message)
        self.save()

    def start(self):
        def loop():
            while not self.stop.is_set():
                try: self.tick()
                except Exception:
                    with self.lock:
                        self.state.update(lastStatus='error', message='Daily recap paused after an unexpected error. Check available disk space and restart the app.')
                    return
                self.stop.wait(1 if self.phase else 30)
        self.thread = threading.Thread(target=loop, daemon=True); self.thread.start()

    def close(self):
        self.stop.set()
        self.worker.close()
        if self.thread: self.thread.join(timeout=3)
