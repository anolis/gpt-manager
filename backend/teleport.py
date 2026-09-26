"""Explicit SSH handoff into a selected local project, without overwriting contexts."""
import os
import subprocess
import tempfile
import uuid
from pathlib import Path

try:
    from .manager import atomic_json
    from .workspace_transfer import extract_workspace
    from .session_lock import context_lock
except ImportError:
    from manager import atomic_json
    from workspace_transfer import extract_workspace
    from session_lock import context_lock


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


class Teleport:
    def __init__(self, remote):
        self.remote = remote
        self.manager = remote.manager

    def run(self, id, folder, copy_workspace=False):
        c = self.remote.context(id)
        with context_lock(c['provider'], c['sessionId']):
            return self._run(id, folder, copy_workspace)

    def _run(self, id, folder, copy_workspace=False):
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
        # Never replace a local session, even if its history currently looks identical.
        self.manager.scan()
        if any(x['origin'] == 'local' and x['provider'] == c['provider'] and x['sessionId'] == c['sessionId'] for x in self.manager.contexts.values()):
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
        self.remote.progress('Reserving the source conversation…')
        try:
            self.remote.request(endpoint, {**request, 'operation': 'reserve', 'target': self.remote.machine['name']})
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
                self.remote.refresh(endpoint['id'])
                return {'id': local['id'], 'library': self.remote.library(), 'warning': warning}
        except Exception as error:
            if not restored:
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
