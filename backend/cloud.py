"""Cloud account connections and append-only context snapshot synchronization.

Only the connector talks to cloud services. Provider stores are never synchronized
in place. Secrets stay in a private connector config and never enter archives.
"""
from __future__ import annotations

import base64
import configparser
import copy
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path

try:
    from .manager import MAX_TOTAL, atomic_json, digest, now, read_json
except ImportError:
    from manager import MAX_TOTAL, atomic_json, digest, now, read_json

CLOUDS = {'google': ('Google Drive', 'drive'), 'onedrive': ('OneDrive', 'onedrive'), 'icloud': ('iCloud Drive', 'iclouddrive')}
RCLONE_VERSION = 'v1.75.1'
REMOTE_DIR = 'GPT Manager/v1'
ARCHIVE_NAME = re.compile(r'^[a-f0-9]{32}-[a-f0-9]{32}\.gptctx$')


class Connector:
    """Pinned official rclone, authenticated on an ephemeral loopback port."""
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        self.config = self.directory / 'accounts.conf'
        self.process = None
        self.url = None
        self.password = secrets.token_urlsafe(32)
        self.ready = threading.Event()
        self.process_lock = threading.RLock()

    def binary(self):
        override = os.environ.get('GPT_MANAGER_RCLONE')
        if override:
            path = Path(override).resolve()
            if not path.is_file():
                raise ValueError('Configured connector executable is missing')
            return path
        name = 'rclone.exe' if os.name == 'nt' else 'rclone'
        target = self.directory / 'tools' / RCLONE_VERSION / name
        if target.is_file():
            return target
        system = {'Linux': 'linux', 'Darwin': 'osx', 'Windows': 'windows'}.get(platform.system())
        machine = {'x86_64': 'amd64', 'AMD64': 'amd64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(platform.machine())
        if not system or not machine:
            raise ValueError('Cloud sign-in is unavailable on this architecture. Use a synced folder instead.')
        filename = f'rclone-{RCLONE_VERSION}-{system}-{machine}.zip'
        base = f'https://downloads.rclone.org/{RCLONE_VERSION}/'
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=target.parent) as temp:
            package = Path(temp) / 'connector.zip'
            self.download(base + filename, package, 160 * 1024**2)
            sums = Path(temp) / 'SHA256SUMS'
            self.download(base + 'SHA256SUMS', sums, 1024**2)
            expected = next((line.split()[0] for line in sums.read_text().splitlines() if len(line.split()) == 2 and line.split()[-1].lstrip('*') == filename), None)
            if not expected or digest(package) != expected:
                raise ValueError('Cloud connector download failed checksum verification')
            with zipfile.ZipFile(package) as archive:
                prefix = filename[:-4] + '/'
                executable = Path(temp) / name
                executable.write_bytes(archive.read(prefix + name))
                (target.parent / 'RCLONE-LICENSE.txt').write_bytes(archive.read(prefix + 'README.txt'))
            executable.chmod(0o700)
            executable.replace(target)
        return target

    @staticmethod
    def download(url, target, limit):
        with urllib.request.urlopen(url, timeout=60) as response, target.open('xb') as out:
            size = 0
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    raise ValueError('Download exceeded the permitted size')
                out.write(chunk)

    def start(self):
        with self.process_lock:
            if self.process and self.process.poll() is None:
                return
            executable = self.binary()
            self.ready.clear()
            self.url = None
            self.config.touch(mode=0o600, exist_ok=True)
            self.config.chmod(0o600)
            # Ignore ambient rclone settings, which could redirect remotes or turn on secret logging.
            env = {k: v for k, v in os.environ.items() if not k.startswith('RCLONE_')}
            env['RCLONE_RC_PASS'] = self.password
            self.process = subprocess.Popen([str(executable), 'rcd', '--rc-addr', '127.0.0.1:0', '--rc-user', 'gpt-manager', '--config', str(self.config), '--log-level', 'NOTICE', '--contimeout', '20s', '--timeout', '5m', '--retries', '2'], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            proc = self.process
            def drain():
                for line in proc.stderr:
                    match = re.search(r'http://127\.0\.0\.1:[0-9]+', line)
                    if match:
                        self.url = match.group(0)
                        self.ready.set()
                    # Never forward provider output or credentials to logs/the renderer.
                self.ready.set()
            threading.Thread(target=drain, daemon=True).start()
            if not self.ready.wait(15) or not self.url:
                self.close()
                raise ValueError('The cloud connector could not start its private localhost service')

    def call(self, method, params):
        if method not in ('config/create', 'config/update', 'config/delete', 'operations/list', 'operations/mkdir', 'operations/copyfile', 'operations/movefile'):
            raise ValueError('Unsupported connector operation')
        self.start()
        auth = base64.b64encode(('gpt-manager:' + self.password).encode()).decode()
        request = urllib.request.Request(self.url + '/' + method, data=json.dumps(params).encode(), headers={'Authorization': 'Basic ' + auth, 'Content-Type': 'application/json'})
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=360) as response:
                result = json.load(response)
        except urllib.error.HTTPError as e:
            # RC errors can echo input parameters, including passwords; do not surface the body.
            raise ValueError(f'Cloud request failed (connector status {e.code}). Check the account, connection, or sign in again.') from None
        except (urllib.error.URLError, TimeoutError):
            raise ValueError('Cloud connection interrupted or sign-in timed out. Retry when connected.') from None
        finally:
            if self.config.exists():
                self.config.chmod(0o600)
        return result

    def close(self):
        with self.process_lock:
            if self.process:
                self.process.terminate()
                try:
                    self.process.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=4)
                self.process = None
            self.url = None


class CloudSync:
    def __init__(self, manager, manager_lock=None, connector=None):
        self.manager = manager
        self.lock = threading.RLock()
        self.manager_lock = manager_lock or threading.RLock()
        self.directory = manager.data / 'cloud'
        self.directory.mkdir(exist_ok=True)
        self.directory.chmod(0o700)
        self.file = self.directory / 'sync.json'
        self.state = read_json(self.file, {'device': uuid.uuid4().hex, 'targets': [], 'received': {}})
        self.connector = connector or Connector(self.directory)
        self.job = None
        self.pending = None
        self.stopping = False
        self.save()

    def save(self):
        atomic_json(self.file, self.state)
        self.file.chmod(0o600)

    def oauth_config(self):
        # This is build/maintainer configuration, never an end-user API-key form.
        config = read_json(self.directory / 'oauth-clients.json', {})
        release = read_json(Path(__file__).resolve().parents[1] / 'cloud-oauth.json', {})
        return {**release, **config}

    def configure_google(self, config):
        if self.job and self.job['status'] == 'running':
            raise ValueError('Wait for the current cloud operation to finish')
        desktop = config.get('installed', {})
        if not isinstance(desktop, dict) or not str(desktop.get('client_id', '')).endswith('.apps.googleusercontent.com') or not desktop.get('client_secret'):
            raise ValueError('Choose an OAuth Desktop app JSON downloaded from Google Cloud')
        # OAuth endpoints from the imported JSON are deliberately ignored.
        clients = self.oauth_config()
        clients['google'] = {'client_id': desktop['client_id'], 'client_secret': desktop['client_secret']}
        atomic_json(self.directory / 'oauth-clients.json', clients)
        (self.directory / 'oauth-clients.json').chmod(0o600)
        return self.status()

    def status(self):
        with self.lock:
            targets = [{k: v for k, v in t.items() if k not in ('fingerprints',)} for t in self.state['targets']]
            return {'targets': copy.deepcopy(targets), 'job': copy.deepcopy(self.job), 'googleReady': bool(self.oauth_config().get('google', {}).get('client_id')), 'pending': self.pending['id'] if self.pending else None}

    def _launch(self, label, function):
        with self.lock:
            if self.job and self.job['status'] == 'running':
                raise ValueError('A cloud operation is already running')
            job = {'id': uuid.uuid4().hex, 'label': label, 'status': 'running', 'message': label}
            self.job = job
        def work():
            try:
                result = function()
                with self.lock:
                    job.update(status='complete', result=result)
            except Exception as e:
                with self.lock:
                    job.update(status='error', error=str(e) if isinstance(e, ValueError) else 'Cloud operation failed. Check connectivity and available disk space, then retry.')
        threading.Thread(target=work, daemon=True).start()
        return self.status()

    def connect(self, provider, apple_id='', password=''):
        if provider not in CLOUDS:
            raise ValueError('Unknown cloud provider')
        if self.job and self.job['status'] == 'running':
            raise ValueError('A cloud operation is already running')
        if self.pending:
            raise ValueError('Finish or cancel the current sign-in first')
        if provider == 'google' and not self.oauth_config().get('google', {}).get('client_id'):
            raise ValueError('Google sign-in needs the maintainer’s OAuth app configuration. A synced folder is available meanwhile.')
        if provider == 'icloud' and (not apple_id.strip() or not password):
            raise ValueError('Enter your Apple Account and password')
        connection = {'id': uuid.uuid4().hex, 'provider': provider, 'mode': 'account'}
        self.pending = connection
        parameters = {}
        if provider == 'google':
            parameters.update(self.oauth_config()['google'])
            parameters['scope'] = 'drive.file'
        if provider == 'onedrive':
            parameters['disable_site_permission'] = 'true'
        if provider == 'icloud':
            parameters.update(apple_id=apple_id.strip(), password=password, service='drive')
        def begin():
            self._message('Preparing cloud connector…')
            result = self.connector.call('config/create', {'name': 'gpt_' + connection['id'], 'type': CLOUDS[provider][1], 'parameters': parameters, 'opt': {'nonInteractive': True, 'obscure': True}})
            parameters.clear()
            return self._advance(connection, result)
        return self._launch('Connecting ' + CLOUDS[provider][0], begin)

    def _message(self, message):
        with self.lock:
            if self.job:
                self.job['message'] = message

    def _advance(self, connection, result):
        for _ in range(15):
            if self.pending is not connection or self.stopping:
                raise ValueError('Sign-in canceled')
            option = result.get('Option') or {}
            state = result.get('State', '')
            if result.get('Error'):
                # Avoid returning opaque provider error strings that can contain credentials.
                error = 'The provider rejected that response. Please try again.'
            else:
                error = ''
            if not state:
                self.connector.call('operations/mkdir', {'fs': 'gpt_' + connection['id'] + ':', 'remote': REMOTE_DIR})
                target = {**{k: v for k, v in connection.items() if k != 'state'}, 'label': CLOUDS[connection['provider']][0], 'auto': False, 'selection': [], 'allLocal': False, 'fingerprints': {}, 'lastSync': None}
                with self.lock:
                    self.state['targets'].append(target)
                    self.pending = None
                    self.save()
                return {'connected': True}
            key = option.get('Name')
            defaults = {'config_is_local': 'true', 'config_type': 'onedrive', 'config_change_team_drive': 'false', 'config_driveok': 'true'}
            if key in defaults and not error:
                self._message('Finish signing in in your browser…' if key == 'config_is_local' else 'Completing account setup…')
                result = self.connector.call('config/update', {'name': 'gpt_' + connection['id'], 'parameters': {}, 'opt': {'nonInteractive': True, 'continue': True, 'state': state, 'result': defaults[key]}})
                continue
            connection['state'] = state
            # Only presentation-safe fields are exposed. Never include Result, token, cookies, or defaults for secrets.
            return {'question': {'name': key, 'help': option.get('Help', ''), 'password': bool(option.get('IsPassword') or key == 'config_2fa'), 'examples': [{'value': str(x['Value']), 'label': x.get('Help', x['Value'])} for x in option.get('Examples', [])], 'default': '' if option.get('IsPassword') or option.get('Sensitive') else str(option.get('DefaultStr', '')), 'required': bool(option.get('Required')), 'error': error}}
        raise ValueError('The cloud provider returned too many setup steps')

    def answer(self, answer):
        connection = self.pending
        if not connection or not connection.get('state'):
            raise ValueError('No sign-in question is pending')
        if not isinstance(answer, str) or len(answer) > 10000:
            raise ValueError('Invalid sign-in response')
        def proceed():
            result = self.connector.call('config/update', {'name': 'gpt_' + connection['id'], 'parameters': {}, 'opt': {'nonInteractive': True, 'continue': True, 'state': connection['state'], 'result': answer}})
            return self._advance(connection, result)
        return self._launch('Completing sign-in…', proceed)

    def cancel(self):
        with self.lock:
            connection = self.pending
            self.pending = None
        if connection:
            self.connector.close()
            self._remove_config(connection['id'])
        return self.status()

    def folder(self, provider, path):
        if provider not in CLOUDS:
            raise ValueError('Unknown cloud provider')
        root = Path(path).resolve()
        if not root.is_dir():
            raise ValueError('Choose an existing synced folder')
        if root == self.manager.home or root == self.manager.data or root.is_relative_to(self.manager.data):
            raise ValueError('Choose a dedicated cloud-synced folder, outside manager storage')
        with self.lock:
            if any(t.get('path') == str(root) for t in self.state['targets']):
                raise ValueError('That folder is already connected')
            self.state['targets'].append({'id': uuid.uuid4().hex, 'provider': provider, 'mode': 'folder', 'label': CLOUDS[provider][0] + ' folder', 'path': str(root), 'auto': False, 'selection': [], 'allLocal': False, 'fingerprints': {}, 'lastSync': None})
            self.save()
        return self.status()

    def target(self, id):
        target = next((t for t in self.state['targets'] if t['id'] == id), None)
        if not target:
            raise ValueError('Cloud connection is no longer available')
        return target

    def configure(self, id, selection=None, allLocal=None, auto=None):
        with self.lock:
            if self.job and self.job['status'] == 'running':
                raise ValueError('Wait for the current cloud operation to finish')
            target = self.target(id)
            if selection is not None:
                if not isinstance(selection, list) or any(not isinstance(x, str) for x in selection):
                    raise ValueError('Invalid context selection')
                target['selection'] = list(dict.fromkeys(selection))
                target['allLocal'] = False
            if allLocal is not None:
                target['allLocal'] = bool(allLocal)
            if auto is not None:
                target['auto'] = bool(auto)
            self.save()
        return self.status()

    def _remove_config(self, id):
        config = self.directory / 'accounts.conf'
        if config.exists():
            parser = configparser.RawConfigParser()
            parser.read(config)
            parser.remove_section('gpt_' + id)
            with config.open('w') as out:
                parser.write(out)
            config.chmod(0o600)

    def disconnect(self, id):
        with self.lock:
            if self.job and self.job['status'] == 'running':
                raise ValueError('Wait for the current cloud operation to finish')
            target = self.target(id)
            self.connector.close()
            if target['mode'] == 'account':
                self._remove_config(id)
            self.state['targets'].remove(target)
            self.save()
        return self.status()

    def _filesystem(self, target):
        if target['mode'] == 'account':
            return 'gpt_' + target['id'] + ':' + REMOTE_DIR
        root = Path(target['path'])
        if not root.is_dir():
            raise ValueError('The synced folder is offline or unavailable')
        destination = root / 'GPT Manager' / 'v1'
        if not destination.resolve().is_relative_to(root.resolve()) or any(p.is_symlink() for p in (destination, destination.parent)):
            raise ValueError('The sync destination contains a symlink')
        destination.mkdir(parents=True, exist_ok=True)
        return destination

    def _fingerprint(self, ctx):
        rows = []
        for path, relative in self.manager.native_files(ctx):
            info = path.stat()
            rows.append((relative, info.st_size, info.st_mtime_ns))
            if path.suffix == '.db':
                wal = path.with_name(path.name + '-wal')
                if wal.exists():
                    info = wal.stat()
                    rows.append((relative + '-wal', info.st_size, info.st_mtime_ns))
        metadata = self.manager.annotations.get(ctx['id'], {})
        return hashlib.sha256(json.dumps([rows, metadata], sort_keys=True).encode()).hexdigest()

    def sync(self, id):
        target = self.target(id)
        if self.pending:
            raise ValueError('Finish signing in before syncing')
        return self._launch('Syncing ' + target['label'], lambda: self._sync(target))

    def _sync(self, target):
        exported, imported, skipped = 0, 0, 0
        errors = []
        try:
            destination = self._filesystem(target)
            with self.manager_lock:
                self.manager.scan()
                ids = [c['id'] for c in self.manager.contexts.values() if c['origin'] == 'local' and (not self.manager.annotations.get(c['id'], {}).get('archived') if target['allLocal'] else c['id'] in target['selection'])]
            with tempfile.TemporaryDirectory(dir=self.directory) as temporary:
                temporary = Path(temporary)
                for id in ids:
                    self._message(f'Preparing snapshot {exported + 1}…')
                    with self.manager_lock:
                        ctx = self.manager.get(id)
                        fingerprint = self._fingerprint(ctx)
                        if target['fingerprints'].get(id) == fingerprint:
                            continue
                        name = self.state['device'] + '-' + uuid.uuid4().hex + '.gptctx'
                        archive = temporary / name
                        self.manager.export([id], archive)
                    self._message('Uploading a context snapshot…')
                    if target['mode'] == 'folder':
                        partial = destination / (name + '.partial')
                        try:
                            with archive.open('rb') as inp, partial.open('xb') as out:
                                partial.chmod(0o600)
                                shutil.copyfileobj(inp, out)
                            if (destination / name).exists():
                                raise ValueError('Snapshot filename conflict')
                            partial.replace(destination / name)
                        finally:
                            partial.unlink(missing_ok=True)
                    else:
                        self.connector.call('operations/copyfile', {'srcFs': str(temporary), 'srcRemote': name, 'dstFs': destination, 'dstRemote': name + '.partial'})
                        self.connector.call('operations/movefile', {'srcFs': destination, 'srcRemote': name + '.partial', 'dstFs': destination, 'dstRemote': name})
                    with self.lock:
                        target['fingerprints'][id] = fingerprint
                        self.save()
                    archive.unlink()
                    exported += 1
                self._message('Checking for snapshots from other machines…')
                if target['mode'] == 'folder':
                    entries = [{'Name': p.name, 'Size': p.stat().st_size} for p in destination.iterdir() if p.is_file() and not p.is_symlink()]
                else:
                    result = self.connector.call('operations/list', {'fs': destination, 'remote': '', 'opt': {'recurse': False, 'filesOnly': True, 'noModTime': True}})
                    entries = result.get('list', [])
                if len(entries) > 10000:
                    raise ValueError('This sync folder has more than 10,000 snapshots. Archive older snapshots before syncing.')
                received = self.state['received']
                for entry in entries:
                    name = entry.get('Name', '')
                    if not ARCHIVE_NAME.fullmatch(name) or name.startswith(self.state['device'] + '-'):
                        continue
                    ledger_key = target['id'] + '/' + name
                    if ledger_key in received:
                        skipped += 1
                        continue
                    if entry.get('Size', -1) < 0 or entry['Size'] > MAX_TOTAL:
                        errors.append('A snapshot exceeds the supported archive size')
                        continue
                    self._message('Downloading and validating a context snapshot…')
                    archive = temporary / name
                    try:
                        if target['mode'] == 'folder':
                            shutil.copyfile(destination / name, archive)
                        else:
                            self.connector.call('operations/copyfile', {'srcFs': destination, 'srcRemote': name, 'dstFs': str(temporary), 'dstRemote': name})
                        checksum = digest(archive)
                        if checksum not in received.values():
                            with self.manager_lock:
                                self.manager.import_archive(archive)
                            imported += 1
                        with self.lock:
                            received[ledger_key] = checksum
                            self.save()
                    except (ValueError, zipfile.BadZipFile, KeyError, TypeError):
                        errors.append('A snapshot was not imported because validation or download failed; it will be retried')
                    finally:
                        archive.unlink(missing_ok=True)
            with self.lock:
                target['lastSync'] = now()
                target['lastAttempt'] = time.time()
                target['lastError'] = '; '.join(dict.fromkeys(errors))
                self.save()
            return {'exported': exported, 'imported': imported, 'skipped': skipped, 'warnings': errors}
        except Exception:
            with self.lock:
                target['lastAttempt'] = time.time()
                target['lastError'] = 'Sync did not finish. Check connectivity, storage space, and account access, then retry.'
                self.save()
            raise

    def remap_context(self, previous, current):
        with self.lock:
            for target in self.state['targets']:
                target['selection'] = [current if id == previous else id for id in target['selection']]
                target['fingerprints'].pop(previous, None)
            self.save()

    def tick(self):
        if self.pending or (self.job and self.job['status'] == 'running'):
            return self.status()
        for target in self.state['targets']:
            if target['auto'] and time.time() - target.get('lastAttempt', 0) >= 15 * 60:
                return self.sync(target['id'])
        return self.status()

    def close(self):
        self.stopping = True
        self.pending = None
        self.connector.close()
