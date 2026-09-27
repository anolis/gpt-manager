"""Provider-reported quota snapshots; no token-to-quota estimates."""
import copy
import json
import math
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from datetime import datetime
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError
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


def reset_timestamp(value):
    if isinstance(value, str):
        try: return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        except (ValueError, OverflowError): return None
    return value


def claude_account_windows(value):
    result = []
    for key, label in (('five_hour', '5 hour window'), ('seven_day', '7 day window'), ('seven_day_opus', 'Opus · 7 day window'), ('seven_day_sonnet', 'Sonnet · 7 day window'), ('seven_day_cowork', 'Cowork · 7 day window'), ('extra_usage', 'Extra usage budget')):
        bucket = value.get(key)
        if not isinstance(bucket, dict): continue
        item = quota(label, {'usedPercent': bucket.get('utilization'), 'resetsAt': reset_timestamp(bucket.get('resets_at'))})
        if item: result.append(item)
    return result


def antigravity_windows(value):
    command = value.get('command', {})
    if value.get('num_turns') != 0 or command.get('name') not in ('usage', '/usage', 'quota', '/quota'):
        raise ValueError('Antigravity did not return a read-only quota report.')
    result = []
    for group in command.get('data', {}).get('groups', [])[:50]:
        for bucket in group.get('buckets', [])[:50]:
            remaining = bucket.get('remaining_fraction')
            if bucket.get('disabled') or bucket.get('enabled') is False: continue
            if isinstance(remaining, bool) or not isinstance(remaining, (int, float)) or not math.isfinite(remaining) or not 0 <= remaining <= 1: continue
            label = ' · '.join(str(x) for x in (group.get('name'), bucket.get('name') or bucket.get('id'), bucket.get('window')) if x)
            item = quota(label, {'usedPercent': (1 - remaining) * 100, 'resetsAt': reset_timestamp(bucket.get('reset_time'))})
            if item: result.append(item)
    return result


class UsageUnavailable(ValueError):
    """A deliberate, credential-free explanation suitable for the UI."""


class NoAuthRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


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
        self.claude = {'id': 'claude', 'windows': [], 'message': 'Refresh to read your Claude subscription limits.'}
        self.antigravity = {'id': 'antigravity', 'windows': [], 'message': 'Refresh to read Antigravity model quotas.'}
        self.claude_retry_at = 0

    def status(self):
        with self.lock:
            cached = read_json(self.directory / 'claude.json', {})
            claude = copy.deepcopy(self.claude)
            if cached.get('updated', 0) > claude.get('updated', 0):
                claude.update(windows=cached.get('windows', []), updated=cached.get('updated'), source=cached.get('source'))
                claude['message'] += ' Displaying the latest report from a local manager terminal.'
            config = read_json(self.directory / 'settings.json', {})
            return {'running': self.running, 'claudeMeter': bool(config.get('claudeMeter')), 'providers': [copy.deepcopy(self.codex),
                claude,
                {'id': 'gemini', 'windows': [], 'message': 'Automatic quota reporting is not integrated yet. Open Gemini and use /stats model for its usage and quota details.'},
                copy.deepcopy(self.antigravity)]}

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
                for provider, read, parse, message in (
                    ('codex', self._codex_read, codex_windows, 'Live account quota reported by the local Codex CLI.'),
                    ('claude', self._claude_read, claude_account_windows, 'Live subscription limits from your local Claude login.'),
                    ('antigravity', self._antigravity_read, antigravity_windows, 'Live model quotas reported by Antigravity CLI.')):
                    if self.closed: break
                    with self.lock: getattr(self, provider)['checking'] = True
                    try:
                        windows = parse(read())
                        with self.lock: setattr(self, provider, {'id': provider, 'windows': windows, 'updated': time.time(), 'message': message if windows else 'This account did not report quota percentages. API-key or unsupported accounts may not expose subscription limits.'})
                    except Exception as error:
                        reason = str(error) if isinstance(error, UsageUnavailable) else f'Could not refresh {provider.capitalize()} usage. Check sign-in and CLI version in AI setup.'
                        with self.lock: getattr(self, provider).update(checking=False, message=reason + ' Any displayed numbers are the last successful report.')
            finally:
                with self.lock: self.running = False
        threading.Thread(target=work, daemon=True).start()
        return self.status()

    def _claude_read(self):
        if time.time() < self.claude_retry_at:
            raise UsageUnavailable('Claude temporarily rate-limited quota checks. Retrying after the cooldown.')
        env = self.setup.environment()
        token = env.get('CLAUDE_CODE_OAUTH_TOKEN')
        if not token:
            path = Path(env.get('CLAUDE_CONFIG_DIR', str(Path.home() / '.claude'))) / '.credentials.json'
            if path.is_file() and path.stat().st_size <= 1024 * 1024:
                token = read_json(path, {}).get('claudeAiOauth', {}).get('accessToken')
        if not isinstance(token, str) or not token:
            raise UsageUnavailable('No readable Claude OAuth login found. Sign in in AI setup, or enable the optional terminal meter. API keys do not provide subscription quotas.')
        # Match the CLI's read-only endpoint; never forward the login to redirects/custom servers.
        request = Request('https://api.anthropic.com/api/oauth/usage', headers={'Authorization': 'Bearer ' + token, 'anthropic-beta': 'oauth-2025-04-20', 'User-Agent': 'gpt-manager/0.6.0'})
        try:
            with build_opener(NoAuthRedirect()).open(request, timeout=20) as response:
                raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024: raise ValueError('Oversized usage response')
            return json.loads(raw)
        except HTTPError as error:
            if error.code in (401, 403): raise UsageUnavailable('Claude login expired or cannot read subscription limits. Sign in again in AI setup.') from None
            if error.code == 429:
                try: delay = max(60, min(3600, int(error.headers.get('Retry-After', 300))))
                except (ValueError, TypeError): delay = 300
                self.claude_retry_at = time.time() + delay
                raise UsageUnavailable('Claude temporarily rate-limited quota checks. Retrying after the cooldown.') from None
            raise UsageUnavailable('Claude’s usage service is unavailable. Try again later.') from None

    def _cli_output(self, argv, timeout=30):
        with tempfile.TemporaryDirectory(prefix='gpt-usage-') as directory, tempfile.TemporaryFile() as out:
            with self.lock:
                if self.closed: raise ValueError('Closed')
                proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.DEVNULL, cwd=directory, env=self.setup.environment(), start_new_session=os.name != 'nt', creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                self.process = proc
            try:
                deadline = time.monotonic() + timeout
                while proc.poll() is None:
                    if self.closed or time.monotonic() > deadline or os.fstat(out.fileno()).st_size > 1024 * 1024: raise ValueError('Usage command timed out or exceeded its output limit')
                    time.sleep(.1)
                if proc.returncode: raise ValueError('Usage command failed')
                out.seek(0); raw = out.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024: raise ValueError('Oversized usage response')
                return raw.decode('utf-8')
            finally:
                stop_process(proc)
                with self.lock: self.process = None

    def _antigravity_read(self):
        argv = self.setup.command('antigravity')
        version = self._cli_output([*argv, '--version'], timeout=10).strip()
        match = re.fullmatch(r'(?:agy\s+)?v?(\d+)\.(\d+)\.(\d+)(?:[+][\w.-]+)?', version)
        # Older versions treat /usage as a model prompt. Never send it to those versions.
        if not match or tuple(map(int, match.groups())) < (1, 1, 11):
            raise UsageUnavailable('Update Antigravity CLI to 1.1.11 or newer in AI setup for read-only quota reporting.')
        return json.loads(self._cli_output([*argv, '-p', '/usage', '--output-format', 'json']))

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
