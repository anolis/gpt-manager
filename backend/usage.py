"""Provider-reported quota snapshots; no token-to-quota estimates."""
import copy
import json
import math
import os
import queue
import subprocess
import tempfile
import threading
import time
from pathlib import Path
try:
    from .manager import atomic_json, read_json
    from .providers import stop_process
except ImportError:
    from manager import atomic_json, read_json
    from providers import stop_process


def quota(label, value, used_key='usedPercent', reset_key='resetsAt'):
    if not isinstance(value, dict): return None
    used, reset = value.get(used_key), value.get(reset_key)
    if isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(used) or used < 0: return None
    if isinstance(reset, bool) or not isinstance(reset, (int, float)) or not math.isfinite(reset) or reset <= 0: reset = None
    return {'label': str(label)[:100], 'remaining': max(0, 100 - min(100, used)), 'used': used, 'resetsAt': reset, 'windowMinutes': value.get('windowDurationMins')}


def codex_windows(response):
    buckets = response.get('rateLimitsByLimitId') or {'codex': response.get('rateLimits', {})}
    windows = []
    for key, bucket in list(buckets.items())[:20]:
        if not isinstance(bucket, dict): continue
        for period in ('primary', 'secondary'):
            value = bucket.get(period)
            if not value: continue
            minutes = value.get('windowDurationMins')
            label = bucket.get('limitName') or key
            duration = f'{minutes // 1440:g} day' if isinstance(minutes, (int, float)) and minutes >= 1440 else f'{minutes / 60:g} hour' if isinstance(minutes, (int, float)) and minutes >= 60 else f'{minutes} minute' if minutes else period
            item = quota(f'{label} · {duration} window', value)
            if item: windows.append(item)
    return windows


def claude_windows(value):
    result = []
    for key, label in (('five_hour', '5 hour window'), ('seven_day', '7 day window'), ('spend_limit', 'Spend limit')):
        item = quota(label, value.get(key), 'used_percentage', 'resets_at')
        if item: result.append(item)
    return result


class Usage:
    def __init__(self, data, setup):
        self.directory = Path(data) / 'usage'; self.directory.mkdir(exist_ok=True)
        self.setup = setup
        self.lock = threading.RLock()
        self.running = False
        self.process = None
        self.closed = False
        self.last_refresh = 0
        self.codex = {'id': 'codex', 'windows': [], 'message': 'Refresh to read the signed-in Codex account’s limits.'}

    def status(self):
        with self.lock:
            claude = read_json(self.directory / 'claude.json', {})
            config = read_json(self.directory / 'settings.json', {})
            return {'running': self.running, 'claudeMeter': bool(config.get('claudeMeter')), 'providers': [copy.deepcopy(self.codex),
                {'id': 'claude', 'windows': claude.get('windows', []), 'updated': claude.get('updated'), 'message': 'Last report from a local manager terminal. New data arrives after Claude responds; Pro/Max or supported gateway limits only.', 'source': claude.get('source')},
                {'id': 'gemini', 'windows': [], 'message': 'Automatic quota reporting is not integrated yet. Open Gemini and use /stats model for its usage and quota details.'},
                {'id': 'antigravity', 'windows': [], 'message': 'This adapter does not expose an account quota feed. Check usage in Antigravity.'}]}

    def configure(self, claudeMeter):
        if not isinstance(claudeMeter, bool): raise ValueError('Choose whether to enable the Claude meter.')
        atomic_json(self.directory / 'settings.json', {'claudeMeter': claudeMeter})
        return self.status()

    def refresh(self):
        with self.lock:
            if self.closed or self.running or time.monotonic() - self.last_refresh < 30: return self.status()
            self.running = True; self.last_refresh = time.monotonic()
        def work():
            try:
                response = self._codex_read()
                with self.lock: self.codex = {'id': 'codex', 'windows': codex_windows(response), 'updated': time.time(), 'message': 'Live account quota reported by the local Codex CLI. Limits may be shared with other Codex clients.'}
            except Exception:
                with self.lock: self.codex['message'] = 'Could not refresh. Check Codex sign-in and CLI version in AI setup. API-key accounts may not provide subscription quotas. Any displayed numbers are the last successful report.'
            finally:
                with self.lock: self.running = False
        threading.Thread(target=work, daemon=True).start()
        return self.status()

    def _codex_read(self):
        argv = self.setup.command('codex')
        messages = queue.Queue(maxsize=256)
        with tempfile.TemporaryDirectory(prefix='gpt-usage-') as directory:
            with self.lock:
                if self.closed: raise ValueError('Closed')
                proc = subprocess.Popen([*argv, 'app-server', '--listen', 'stdio://'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=directory, env=self.setup.environment(), start_new_session=os.name != 'nt', creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                self.process = proc
            def read():
                try:
                    for _ in range(256):
                        line = proc.stdout.readline(1024 * 1024 + 1)
                        if not line or len(line) > 1024 * 1024: break
                        messages.put_nowait(json.loads(line))
                except (ValueError, OSError, queue.Full): pass
                try: messages.put_nowait(None)
                except queue.Full: pass
            thread = threading.Thread(target=read, daemon=True); thread.start()
            def send(value): proc.stdin.write((json.dumps(value) + '\n').encode()); proc.stdin.flush()
            def response(id):
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    value = messages.get(timeout=max(.1, deadline-time.monotonic()))
                    if value is None: raise ValueError('CLI stopped')
                    if value.get('id') != id: continue
                    if 'error' in value: raise ValueError('Provider could not report usage')
                    return value['result']
                raise ValueError('Usage refresh timed out')
            try:
                send({'id': 1, 'method': 'initialize', 'params': {'clientInfo': {'name': 'gpt_manager_usage', 'version': '0.6.0'}, 'capabilities': {}}}); response(1)
                send({'method': 'initialized', 'params': {}})
                send({'id': 2, 'method': 'account/rateLimits/read', 'params': {}})
                return response(2)
            finally:
                stop_process(proc); thread.join(2)
                proc.stdin.close(); proc.stdout.close()
                with self.lock: self.process = None

    def close(self):
        with self.lock: self.closed = True; process = self.process
        stop_process(process)
