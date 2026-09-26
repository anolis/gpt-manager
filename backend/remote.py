"""SSH endpoint discovery and dispatch. Uses existing SSH keys, agent and host trust."""
import base64
import zlib
import copy
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import uuid
import glob
from pathlib import Path

try:
    from .manager import atomic_json
    from .ssh_agent import machine_identity
    from .session_lock import handoff_status, update_handoff
    from .activity import activity_excerpt
except ImportError:
    from manager import atomic_json
    from ssh_agent import machine_identity
    from session_lock import handoff_status, update_handoff
    from activity import activity_excerpt


def ssh_aliases():
    """Suggest literal Host names only; SSH itself remains the config authority."""
    base = Path.home() / '.ssh'
    seen, aliases = set(), set()
    def read(path, depth=0):
        path = Path(path).expanduser()
        if depth > 10 or path in seen or len(seen) >= 100 or not path.is_file():
            return
        seen.add(path)
        if path.stat().st_size > 1024 * 1024:
            return
        for line in path.read_text(errors='replace').splitlines():
            try:
                parts = shlex.split(line, comments=True)
            except ValueError:
                continue
            if not parts:
                continue
            if '=' in parts[0]:
                key, value = parts[0].split('=', 1)
                parts = [key, value, *parts[1:]]
            if parts[0].lower() == 'host':
                aliases.update(x for x in parts[1:] if re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,199}', x))
            elif parts[0].lower() == 'include':
                for pattern in parts[1:]:
                    pattern = os.path.expanduser(pattern)
                    if not os.path.isabs(pattern):
                        pattern = str(base / pattern)
                    for included in sorted(glob.glob(pattern)):
                        read(included, depth + 1)
    read(base / 'config')
    return sorted(aliases, key=str.casefold)


def ssh_arguments(host, operation, terminal=False):
    if not isinstance(host, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@-]{0,199}', host):
        raise ValueError('Use an SSH config alias or user@hostname. Configure ports and keys in ~/.ssh/config.')
    folder = Path(__file__).parent
    sources = [(folder / name).read_text() for name in ('manager.py', 'session_lock.py', 'workspace_transfer.py', 'activity.py', 'ssh_agent.py')]
    # Each source is compiled separately so __future__ imports remain valid.
    bootstrap = "import base64,json,sys\ng={'__name__':'gpt_manager_ssh'}\n"
    for source in sources:
        encoded = base64.b64encode(source.encode()).decode()
        bootstrap += f"exec(compile(base64.b64decode('{encoded}'),'<gpt-manager>','exec'),g)\n"
    request = base64.b64encode(json.dumps(operation).encode()).decode()
    bootstrap += f"request=json.loads(base64.b64decode('{request}'))\n"
    bootstrap += "try:\n result=g['ssh_dispatch'](request)\n"
    bootstrap += " if request['operation']=='resume': sys.exit(result)\n if request['operation'] not in ('export','workspace'): print(json.dumps({'result':result}))\nexcept Exception as e:\n print(json.dumps({'error':str(e)}))\n sys.exit(1)\n"
    compressed = base64.b64encode(zlib.compress(bootstrap.encode(), 9)).decode()
    remote_command = 'python3 -c ' + shlex.quote("import base64,zlib;exec(zlib.decompress(base64.b64decode('" + compressed + "')))")
    if os.name == 'nt' and len(remote_command) > 30000:
        raise ValueError('This remote operation exceeds Windows command limits.')
    return ['-tt' if terminal else '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=8', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2', host, remote_command]


class RemoteLocations:
    def __init__(self, manager, progress=None):
        self.manager = manager
        self.cache = {}
        self.errors = {}
        self.machine = machine_identity()
        self.progress = progress or (lambda message: None)

    def download(self, endpoint, operation, destination):
        executable = shutil.which('ssh')
        if not executable:
            raise ValueError('OpenSSH is not installed.')
        with destination.open('xb') as out, tempfile.TemporaryFile() as err:
            try:
                result = subprocess.run([executable, *ssh_arguments(endpoint['host'], operation)], stdin=subprocess.DEVNULL, stdout=out, stderr=err, timeout=1800)
            except subprocess.TimeoutExpired:
                raise ValueError('Transfer timed out. Source files have been retained.') from None
        if result.returncode:
            if destination.stat().st_size < 65536:
                try:
                    error = json.loads(destination.read_text()).get('error')
                    if error:
                        raise ValueError(str(error)[:1000])
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass
            raise ValueError('Remote transfer failed. Stop external sessions/builds, check space and SSH connectivity, then retry. Workspace symlinks must stay inside the project.')

    def release(self, id):
        c = self.context(id)
        if c['origin'] == 'remote':
            self.request(self.endpoint(c['endpointId']), {'operation': 'release', 'id': c['remoteId'], 'machineId': c['machineId'], 'force': True})
            return self.refresh(c['endpointId'])
        update_handoff(c['provider'], c['sessionId'], None, force=True)
        return self.library()

    def endpoints(self):
        return self.manager.settings.get('sshEndpoints', [])

    def endpoint(self, id):
        endpoint = next((e for e in self.endpoints() if e['id'] == id), None)
        if endpoint is None:
            raise ValueError('SSH endpoint no longer available')
        return endpoint

    def request(self, endpoint, operation):
        executable = shutil.which('ssh')
        if not executable:
            raise ValueError('OpenSSH is not installed or not on PATH.')
        args = ssh_arguments(endpoint['host'], operation)
        # Bound retained output and never expose raw SSH output in the UI.
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            try:
                result = subprocess.run([executable, *args], stdin=subprocess.DEVNULL, stdout=out, stderr=err, timeout=45)
            except subprocess.TimeoutExpired:
                raise ValueError('SSH request timed out. Check the host and network, then refresh.') from None
            if out.tell() > 32 * 1024**2:
                raise ValueError('Remote response exceeded the 32 MiB limit.')
            out.seek(0)
            try:
                response = json.load(out)
            except (ValueError, UnicodeDecodeError):
                raise ValueError('SSH could not read this host. First connect using your system ssh command to verify host trust and key authentication; ensure Python 3.10+ is on its PATH and shell startup is quiet.') from None
            if not isinstance(response, dict) or result.returncode or 'error' in response:
                raise ValueError(str(response.get('error', 'Remote operation failed'))[:1000])
            return response['result']

    def add(self, host, label=''):
        host = host.strip()
        ssh_arguments(host, {'operation': 'scan'})  # validate before persisting
        if any(e['host'] == host for e in self.endpoints()):
            raise ValueError('This SSH endpoint is already configured.')
        if not isinstance(label, str) or len(label) > 100:
            raise ValueError('Endpoint label must be under 100 characters')
        endpoint = {'id': uuid.uuid4().hex, 'host': host, 'label': label.strip() or host}
        self.manager.settings.setdefault('sshEndpoints', []).append(endpoint)
        atomic_json(self.manager.data / 'settings.json', self.manager.settings)
        return self.refresh(endpoint['id'])

    def remove(self, id):
        self.endpoint(id)
        self.manager.settings['sshEndpoints'] = [e for e in self.endpoints() if e['id'] != id]
        atomic_json(self.manager.data / 'settings.json', self.manager.settings)
        self.cache.pop(id, None)
        self.errors.pop(id, None)
        return self.library()

    def refresh(self, id):
        endpoint = self.endpoint(id)
        try:
            result = self.request(endpoint, {'operation': 'scan'})
            if not isinstance(result, dict) or not isinstance(result.get('contexts'), list) or not isinstance(result.get('machine'), dict):
                raise ValueError('Remote host returned an invalid library')
            self.cache[id] = result
            self.errors.pop(id, None)
        except (ValueError, OSError) as error:
            self.errors[id] = str(error)
        return self.library()

    def scan(self):
        self.manager.scan()
        for endpoint in self.endpoints():
            self.refresh(endpoint['id'])
        return self.library()

    def library(self):
        result = copy.deepcopy(self.manager.library())
        result['machine'] = self.machine
        result['sshEndpoints'] = []
        for c in result['contexts']:
            c['machine'] = self.machine['name']
            c['machineId'] = self.machine['id']
            if c['origin'] == 'local':
                c['handoff'] = handoff_status(c['provider'], c['sessionId'])
        for endpoint in self.endpoints():
            cached = self.cache.get(endpoint['id'])
            error = self.errors.get(endpoint['id'])
            result['sshEndpoints'].append({**endpoint, 'connected': bool(cached) and not error, 'error': error, 'machine': cached.get('machine') if cached else None})
            if not cached:
                continue
            for item in cached['contexts']:
                c = dict(item)
                c.update(id='ssh:' + endpoint['id'] + ':' + item['id'], remoteId=item['id'], endpointId=endpoint['id'], origin='remote', machine=cached['machine']['name'], machineId=cached['machine']['id'], endpointLabel=endpoint['label'], offline=bool(error))
                result['contexts'].append(c)
            for loc in cached.get('locations', []):
                result['locations'].append({**loc, 'machine': cached['machine']['name'], 'endpointId': endpoint['id'], 'exists': loc['exists'] and not error})
        groups = {}
        for c in result['contexts']:
            if c['origin'] == 'imported' or c.get('handoff'):
                continue
            groups.setdefault((c['provider'], c['sessionId']), []).append(c)
        for group in groups.values():
            places = set((c['machineId'], c['path']) for c in group)
            if len(places) > 1:
                for c in group:
                    c['copies'] = sorted(set(x['machine'] + (' (imported)' if x['origin'] == 'imported' else '') for x in group if x['id'] != c['id']))
        return result

    def context(self, id):
        c = next((c for c in self.library()['contexts'] if c['id'] == id), None)
        if c is None:
            raise ValueError('Context no longer available')
        return c

    def read(self, operation, id, **params):
        c = self.context(id)
        if c['origin'] != 'remote':
            if operation == 'activity':
                return activity_excerpt(c, **params)
            return getattr(self.manager, operation)(id=id, **params)
        return self.request(self.endpoint(c['endpointId']), {'operation': operation, 'id': c['remoteId'], **params})

    def resume(self, id):
        c = self.context(id)
        if c['origin'] != 'remote':
            raise ValueError('Not an SSH context')
        if c.get('offline'):
            raise ValueError('SSH endpoint is offline. Refresh it before resuming.')
        endpoint = self.endpoint(c['endpointId'])
        return {'args': ssh_arguments(endpoint['host'], {'operation': 'resume', 'id': c['remoteId'], 'machineId': c['machineId']}, terminal=True), 'machine': c['machine']}
