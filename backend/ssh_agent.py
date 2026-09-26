"""Executed from the trusted app bundle over SSH; no remote installation required.

Manager and resume_locked are supplied by the bootstrap. Discovery uses temporary
manager storage and only reads the existing remote settings and native stores.
"""
import hashlib
import os
import platform
import re
import shutil
import socket
import sys
import tempfile
from pathlib import Path


def machine_identity():
    home = str(Path.home())
    machine_file = Path('/etc/machine-id')
    identity = machine_file.read_text().strip() if machine_file.is_file() else socket.gethostname()
    return {'name': socket.gethostname(), 'id': hashlib.sha256((identity + ':' + home).encode()).hexdigest()[:24], 'home': home, 'system': platform.system()}


def source_fingerprint(manager, context):
    files = []
    for path, relative in manager.native_files(manager.get(context['id'])):
        files.append(path)
        if path.suffix == '.db':
            files.extend(p for p in (Path(str(path) + '-wal'), Path(str(path) + '-shm')) if p.exists())
    return {'project': context.get('project'), 'files': [[str(p), p.stat().st_ino, p.stat().st_size, p.stat().st_mtime_ns] for p in sorted(files)]}


def ssh_dispatch(request):
    if sys.version_info < (3, 10):
        raise ValueError('The SSH host needs Python 3.10 or newer.')
    operation = request.get('operation')
    if operation not in ('scan', 'detail', 'files', 'resume', 'reserve', 'export', 'workspace', 'finish', 'release'):
        raise ValueError('Unsupported SSH operation')
    with tempfile.TemporaryDirectory(prefix='gpt-manager-inspect-') as temporary:
        manager = Manager(Path.home(), temporary)
        existing = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'gpt-manager'
        if platform.system() == 'Darwin':
            existing = Path.home() / 'Library/Application Support/gpt-manager'
        manager.settings = read_json(existing / 'settings.json', {})
        manager.annotations = read_json(existing / 'annotations.json', {})
        library = manager.scan()
        machine = machine_identity()
        if operation == 'scan':
            for context in library['contexts']:
                context['handoff'] = handoff_status(context['provider'], context['sessionId'])
            library['machine'] = machine
            library['dataDir'] = str(existing)
            return library
        context_id = request.get('id')
        context = next((c for c in library['contexts'] if c['id'] == context_id), None)
        if not context:
            raise ValueError('Remote context no longer exists. Refresh this endpoint.')
        if operation in ('reserve', 'export', 'workspace', 'finish', 'release'):
            if request.get('machineId') != machine['id']:
                raise ValueError('SSH machine identity changed. Refresh before transferring.')
            provider, sid, token = context['provider'], context['sessionId'], request.get('token')
            if operation == 'reserve':
                reserve_handoff(provider, sid, token, request.get('target'))
                with context_lock(provider, sid, token) as marker:
                    value = json.loads(marker.read_text())
                    value['source'] = source_fingerprint(manager, context)
                    atomic_json(marker, value)
                    marker.chmod(0o600)
                return {'reserved': True}
            if operation in ('export', 'workspace', 'finish'):
                with context_lock(provider, sid, token) as marker:
                    if not marker.exists() or json.loads(marker.read_text()).get('source') != source_fingerprint(manager, context):
                        raise ValueError('Source context changed during handoff. Stop external sessions and retry; the source history has been retained.')
            if operation in ('finish', 'release'):
                return update_handoff(provider, sid, token, finish=operation == 'finish', force=request.get('force', False))
            with context_lock(provider, sid, token):
                output = Path(temporary) / 'transfer.zip'
                if operation == 'export':
                    manager.export([context_id], str(output))
                else:
                    if not context.get('project'):
                        raise ValueError('Context has no recorded project folder')
                    snapshot_workspace(context['project'], output)
                if json.loads(lock_paths(provider, sid)[1].read_text()).get('source') != source_fingerprint(manager, context):
                    raise ValueError('Source context changed during capture. Stop external sessions and retry.')
                with output.open('rb') as source:
                    shutil.copyfileobj(source, sys.stdout.buffer)
                sys.stdout.buffer.flush()
            return None
        if operation == 'detail':
            return manager.detail(context_id, request.get('cursor', 0))
        if operation == 'files':
            return manager.files(context_id)
        if request.get('machineId') != machine['id']:
            raise ValueError('SSH endpoint now identifies a different machine or account. Refresh before resuming.')
        sid, provider = context['sessionId'], context['provider']
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,199}', sid):
            raise ValueError('Context has no valid resume ID')
        commands = {'codex': ['codex', 'resume'], 'claude': ['claude', '--resume'], 'gemini': ['gemini', '--resume'], 'antigravity': ['agy', '--conversation']}
        command, *args = commands[provider]
        executable = shutil.which(command)
        if not executable:
            raise ValueError(f'{command} is not on the remote SSH PATH. Install it or configure the remote shell PATH.')
        if provider == 'antigravity' and not context['root'].endswith('antigravity-cli'):
            raise ValueError('IDE-only Antigravity contexts cannot be resumed with agy.')
        cwd = context.get('project')
        if not cwd or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
            raise ValueError('Remote project folder is missing. Set its project folder in GPT Manager on the remote machine first.')
        env = dict(os.environ)
        if provider == 'codex':
            env['CODEX_HOME'] = context['root']
        if provider == 'claude':
            env['CLAUDE_CONFIG_DIR'] = str(Path(context['root']).parent)
        args.append(sid)
        if provider == 'codex':
            args.extend(['--cd', cwd])
        print(f"\r\nGPT Manager · SSH · {machine['name']} · {cwd}\r\n", flush=True)
        return resume_locked(provider, sid, cwd, executable, args, env)
