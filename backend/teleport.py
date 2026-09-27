"""Explicit SSH handoff, including verified returns to a retained source copy."""
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

try:
    from .manager import atomic_json, read_json, digest
    from .workspace_transfer import extract_workspace
    from .session_lock import context_lock, lock_paths
    from .ssh_agent import source_fingerprint
except ImportError:
    from manager import atomic_json, read_json, digest
    from workspace_transfer import extract_workspace
    from session_lock import context_lock, lock_paths
    from ssh_agent import source_fingerprint


def git_check(folder, pull=False):
    root = Path(folder).resolve()
    if not root.is_dir():
        raise ValueError('Choose an existing local project folder')
    env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0'}
    def git(*args, required=True):
        try:
            result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True, env=env, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            if not required:
                return None
            raise ValueError('Git could not finish. Check network access and Git authentication, then retry.') from None
        if result.returncode:
            if not required:
                return None
            raise ValueError('Git could not finish. Check the repository, upstream and authentication in your terminal.')
        return result.stdout.strip()
    if git('rev-parse', '--show-toplevel', required=False) is None:
        return {'repository': False}
    branch = git('rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{upstream}', required=False)
    if not branch:
        return {'repository': True, 'upstream': None, 'notice': 'No upstream is configured; no pull is available.'}
    git('fetch', '--no-recurse-submodules')
    dirty = bool(git('status', '--porcelain'))
    ahead, behind = map(int, git('rev-list', '--left-right', '--count', 'HEAD...@{upstream}').split())
    if pull:
        if dirty or ahead or not behind:
            raise ValueError('The checkout changed or cannot be fast-forwarded safely. Check Git status and retry.')
        # Fetch already completed; merge the checked upstream without creating a merge commit.
        git('merge', '--ff-only', '@{upstream}')
    return {'repository': True, 'upstream': branch, 'dirty': dirty, 'ahead': ahead, 'behind': behind, 'pulled': pull}


def confirmation_fingerprint(snapshot):
    """Keep nanosecond timestamps/inodes out of JavaScript's lossy numbers."""
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class Teleport:
    def __init__(self, remote):
        self.remote = remote
        self.manager = remote.manager

    def run(self, id, folder, copy_workspace=False, discard_local_changes=False, expected_local=None):
        c = self.remote.context(id)
        if c['origin'] != 'remote':
            raise ValueError('Resume here is for contexts on SSH endpoints.')
        # Reading the token is only permission to inspect a possible return under
        # the lock. The original receipt is verified on the other host before any
        # native files change. Ordinary resume still cannot bypass this marker.
        marker = lock_paths(c['provider'], c['sessionId'], create=False)[1]
        previous = read_json(marker, {})
        with context_lock(c['provider'], c['sessionId'], previous.get('token')):
            return self._run(id, folder, copy_workspace, marker, read_json(marker, {}), discard_local_changes, expected_local)

    def _run(self, id, folder, copy_workspace=False, marker=None, previous=None, discard_local_changes=False, expected_local=None):
        c = self.remote.context(id)
        if c['origin'] != 'remote':
            raise ValueError('Resume here is for contexts on SSH endpoints.')
        endpoint = self.remote.endpoint(c['endpointId'])
        destination = Path(folder).resolve()
        if not destination.is_dir():
            raise ValueError('Choose an existing local project directory.')
        if copy_workspace and any(destination.iterdir()):
            raise ValueError('Choose an empty folder for the remote work folder.')
        if c['machineId'] == self.remote.machine['id']:
            raise ValueError('This endpoint is the current machine. Resume its local context instead.')
        self.manager.scan()
        copies = [x for x in self.manager.library()['contexts'] if x['origin'] == 'local' and x['provider'] == c['provider'] and x['sessionId'] == c['sessionId']]
        retained = None
        local_snapshot = None
        local_changed = False
        if previous:
            if len(copies) != 1:
                raise ValueError(f'Expected one retained local conversation, found {len(copies)}. Refresh and inspect the provider context stores; changing the project folder does not remove stored conversations.')
            if (previous.get('state') not in ('moved', 'reserved')
                    or (previous.get('targetMachineId') and previous['targetMachineId'] != c['machineId'])):
                raise ValueError('This local conversation belongs to another handoff. Select the SSH copy on its recorded destination machine.')
            retained = copies[0]
            local_snapshot = source_fingerprint(self.manager, retained)
            local_changed = previous.get('source') != local_snapshot
            confirmation = confirmation_fingerprint(local_snapshot)
            if discard_local_changes and expected_local != confirmation:
                raise ValueError('The local conversation changed again after confirmation. Stop local provider sessions and retry Resume here.')
            if local_changed and not discard_local_changes:
                # No reservations or writes have occurred. Electron presents the
                # choice, then sends this opaque fingerprint back with confirmation.
                return {'conflict': 'retained-changed', 'localPath': retained['path'],
                        'sourceMachine': c['machine'], 'expectedLocal': confirmation}
        elif copies:
            raise ValueError('A local native copy of this session already exists. Resume that copy or resolve it before teleporting; automatic merging is not supported.')
        if c['provider'] == 'antigravity' and not c['root'].endswith('antigravity-cli'):
            raise ValueError('IDE-only Antigravity state cannot be resumed in a CLI terminal.')
        token = uuid.uuid4().hex
        request = {'id': c['remoteId'], 'machineId': c['machineId'], 'token': token}
        receipt_dir = self.manager.data / 'handoffs'
        receipt_dir.mkdir(exist_ok=True)
        receipt_path = receipt_dir / (token + '.json')
        receipt = {'endpoint': endpoint, 'context': request, 'destination': str(destination), 'state': 'preparing'}
        atomic_json(receipt_path, receipt)
        receipt_path.chmod(0o600)
        restored = False
        removed = []
        backup = None
        self.remote.progress('Reserving the source conversation…')
        try:
            self.remote.request(endpoint, {**request, 'operation': 'reserve', 'target': self.remote.machine['name'],
                                          'targetMachineId': self.remote.machine['id'],
                                          **({'previousToken': previous['token']} if retained else {})})
            with tempfile.TemporaryDirectory(dir=self.manager.data) as temp:
                archive = Path(temp) / 'context.gptctx'
                self.remote.progress('Downloading and validating the conversation…')
                self.remote.download(endpoint, {**request, 'operation': 'export'}, archive)
                preview = self.manager.preview(str(archive))
                if len(preview['contexts']) != 1 or preview['contexts'][0]['sessionId'] != c['sessionId'] or preview['contexts'][0]['provider'] != c['provider']:
                    raise ValueError('Remote archive does not match the selected context.')
                if copy_workspace:
                    self.remote.progress('Copying the remote work folder, including hidden files…')
                    workspace = Path(temp) / 'workspace.zip'
                    self.remote.download(endpoint, {**request, 'operation': 'workspace'}, workspace)
                    # Stage beside destination so installation is a single filesystem rename.
                    with tempfile.TemporaryDirectory(dir=destination.parent, prefix='.gpt-handoff-') as stage:
                        staged = Path(stage) / 'project'
                        staged.mkdir()
                        extract_workspace(workspace, staged)
                        if any(destination.iterdir()):
                            raise ValueError('Destination is no longer empty. No workspace files were replaced.')
                        staged.replace(destination)
                self.remote.progress('Restoring the context into the local provider store…')
                before = set(self.manager.contexts)
                self.manager.import_archive(str(archive))
                imported = next(x for key, x in self.manager.contexts.items() if key not in before and x['origin'] == 'imported' and x['sessionId'] == c['sessionId'])
                roots = [root for provider, root in self.manager.default_roots() if provider == c['provider']]
                root = roots[0]
                root.mkdir(parents=True, exist_ok=True)
                if retained:
                    self.remote.progress('Backing up the retained copy before returning the conversation…')
                    # Keep both a portable archive and byte-for-byte rollback files.
                    # The receipt records every path before the first removal.
                    backup = self.manager.data / 'handoff-backups' / token
                    backup.mkdir(parents=True, mode=0o700)
                    self.manager.export([retained['id']], backup / 'context.gptctx')
                    files = self.manager.native_files(self.manager.get(retained['id']))
                    if any(p.suffix == '.db' and any(Path(str(p) + suffix).exists() for suffix in ('-wal', '-shm')) for p, _ in files):
                        raise ValueError('Close the retained conversation database and checkpoint it before returning this context.')
                    saved = []
                    for index, (path, _) in enumerate(files):
                        copy = backup / str(index)
                        shutil.copy2(path, copy)
                        copy.chmod(0o600)
                        if digest(copy) != digest(path):
                            raise ValueError('Retained context changed during backup. Stop external sessions and retry.')
                        saved.append((path, copy))
                    if local_snapshot != source_fingerprint(self.manager, retained):
                        raise ValueError('Retained context changed during backup. Stop external sessions and retry.')
                    receipt.update(backup=str(backup / 'context.gptctx'), previousHandoff=previous, discardedLocalChanges=local_changed,
                                   retainedFiles=[{'path': str(p), 'backup': str(b)} for p, b in saved], state='return-backed-up')
                    atomic_json(receipt_path, receipt)
                    # Restore to the existing configured store, including custom roots.
                    root = Path(retained['root'])
                    for path, copy in saved:
                        path.unlink()
                        removed.append((path, copy))
                self.manager.restore(imported['id'], str(root), project_folder=str(destination))
                restored = True
                receipt['state'] = 'restored'
                atomic_json(receipt_path, receipt)
                self.manager.scan()
                local = next(x for x in self.manager.contexts.values() if x['origin'] == 'local' and x['provider'] == c['provider'] and x['sessionId'] == c['sessionId'])
                snapshot = preview['contexts'][0]
                self.manager.annotate(local['id'], title=snapshot['title'], notes=snapshot.get('notes', ''), tags=snapshot.get('tags', []))
                self.manager.annotations[local['id']]['project'] = str(destination)
                self.manager.annotations[local['id']]['handoffSource'] = c['machine']
                atomic_json(self.manager.data / 'annotations.json', self.manager.annotations)
                receipt['localId'] = local['id']
                warning = None
                self.remote.progress('Marking the source as handed off…')
                try:
                    self.remote.request(endpoint, {**request, 'operation': 'finish'})
                    receipt['state'] = 'complete'
                except Exception as error:
                    receipt['state'] = 'restored-source-reserved'
                    warning = f'Local restore succeeded, but source confirmation failed: {error}. The source remains reserved. Compare histories before releasing it if the source changed during transfer.'
                atomic_json(receipt_path, receipt)
                if retained:
                    marker.unlink()
                self.remote.refresh(endpoint['id'])
                return {'id': local['id'], 'library': self.remote.library(), 'warning': warning,
                        'backup': str(backup / 'context.gptctx') if backup else None}
        except Exception as error:
            if not restored:
                if removed:
                    try:
                        for path, copy in removed:
                            with path.open('xb') as out, copy.open('rb') as inp:
                                shutil.copyfileobj(inp, out)
                            shutil.copystat(copy, path)
                        self.manager.scan()
                        # Restoring bytes changes inodes; retain the handoff with
                        # the restored copy's fingerprint so another retry is safe.
                        if not local_changed:
                            previous['source'] = source_fingerprint(self.manager, retained)
                        atomic_json(marker, previous)
                        marker.chmod(0o600)
                    except Exception:
                        receipt['state'] = 'return-rollback-needs-attention'
                        atomic_json(receipt_path, receipt)
                        raise ValueError(f'Return interrupted. Both hosts remain reserved; recovery files are at {backup}. Inspect the handoff receipt before retrying.') from None
                try:
                    self.remote.request(endpoint, {**request, 'operation': 'release'})
                    receipt['state'] = 'canceled'
                except Exception:
                    receipt['state'] = 'source-reserved'
                    atomic_json(receipt_path, receipt)
                    raise ValueError(f'{error} The source could not be reached to release its reservation. Reconnect and use Release handoff on the source context.') from None
            else:
                receipt['state'] = 'restored-needs-attention' if restored else 'reserve-uncertain'
            atomic_json(receipt_path, receipt)
            if restored:
                raise ValueError(f'Local context files were restored, but setup needs attention: {error}. The source remains reserved. Refresh the local library before retrying; no files were overwritten.') from None
            raise
