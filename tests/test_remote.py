import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from backend.manager import Manager
from backend.remote import RemoteLocations, ssh_arguments, ssh_aliases
from backend.session_lock import context_lock, reserve_handoff, update_handoff, handoff_status
from backend.teleport import Teleport, git_check
from backend.workspace_transfer import snapshot_workspace, extract_workspace
from backend.network import local_networks, scan_network


class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.local = self.base / 'local'; self.local.mkdir()
        self.host = self.base / 'remote'; self.host.mkdir()
        self.project = self.host / 'project'; self.project.mkdir()
        (self.project / 'source.py').write_text('print("hello")\n')
        self.env = patch.dict(os.environ, {'HOME': str(self.local)}, clear=False); self.env.start()
        self.saved_provider_env = {key: os.environ.pop(key, None) for key in ('CODEX_HOME', 'CLAUDE_CONFIG_DIR', 'GPT_MANAGER_HOME')}
        source = self.host / '.codex/sessions/2026/01/01/rollout.jsonl'
        source.parent.mkdir(parents=True)
        source.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': 'session-remote', 'cwd': str(self.project)}}) + '\n' + json.dumps({'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Remote conversation'}]}}) + '\n')
        self.source = source
        self.manager = Manager(self.local, self.base / 'data'); self.manager.scan()
        self.remote = RemoteLocations(self.manager)
        self.transport = patch.object(self.remote, 'request', side_effect=self.request); self.transport.start()
        self.downloader = patch.object(self.remote, 'download', side_effect=self.download); self.downloader.start()
        self.remote.add('fixture-host')
        self.context = next(c for c in self.remote.library()['contexts'] if c['origin'] == 'remote')

    def tearDown(self):
        self.downloader.stop(); self.transport.stop(); self.env.stop()
        for key, value in self.saved_provider_env.items():
            if value is not None: os.environ[key] = value
        self.tmp.cleanup()

    def execute(self, operation):
        command = shlex.split(ssh_arguments('fixture-host', operation)[-1])
        command[0] = sys.executable
        env = {**os.environ, 'HOME': str(self.host)}
        env.pop('XDG_CONFIG_HOME', None)
        return subprocess.run(command, capture_output=True, env=env, timeout=20)

    def request(self, endpoint, operation):
        result = self.execute(operation)
        try: value = json.loads(result.stdout)
        except ValueError: self.fail(result.stderr.decode() + result.stdout.decode(errors='replace'))
        if 'error' in value: raise ValueError(value['error'])
        self.assertEqual(result.returncode, 0, result.stderr)
        return value['result']

    def download(self, endpoint, operation, destination):
        result = self.execute(operation)
        if result.returncode: raise ValueError(result.stdout.decode(errors='replace'))
        destination.write_bytes(result.stdout)

    def test_remote_library_and_detail_execute_actual_payload(self):
        self.assertEqual(self.context['sessionId'], 'session-remote')
        self.assertNotEqual(self.context['machineId'], self.remote.machine['id'])
        result = self.remote.read('detail', self.context['id'])
        self.assertEqual(result['messages'][0]['content'], 'Remote conversation')
        self.assertFalse((self.host / '.local').exists(), 'Read-only scan created lock storage')

    def test_remote_activity_uses_same_timestamp_filter(self):
        rows = [json.loads(line) for line in self.source.read_text().splitlines()]
        rows[-1]['timestamp'] = '2026-09-25T12:00:00Z'
        self.source.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        result = self.remote.read('activity', self.context['id'], start='2026-09-25T00:00:00Z', end='2026-09-26T00:00:00Z')
        self.assertEqual(result['messages'][0]['content'], 'Remote conversation')

    def test_host_and_shell_argument_validation(self):
        for host in ('-oProxyCommand=evil', 'host;touch /tmp/x', '$(id)', 'a b', ''):
            with self.assertRaises(ValueError): ssh_arguments(host, {'operation': 'scan'})
        args = ssh_arguments('user@host', {'operation': 'detail', 'id': "quote'$(id)"})
        self.assertIn('StrictHostKeyChecking=yes', args)
        self.assertIn('BatchMode=yes', args)
        self.assertEqual(shlex.split(args[-1])[:2], ['python3', '-c'])

    def test_aliases_include_globs_and_ignore_patterns(self):
        root = self.local / '.ssh'; root.mkdir()
        (root / 'config').write_text('Host laptop workstation *.invalid !blocked\n Include hosts/*.conf\nHost=another\n')
        (root / 'hosts').mkdir(); (root / 'hosts/test.conf').write_text('Host "storage"\nInclude config\nHost *\n')
        self.assertEqual(ssh_aliases(), ['another', 'laptop', 'storage', 'workstation'])

    def test_disconnect_keeps_cached_context_offline(self):
        with patch.object(self.remote, 'request', side_effect=ValueError('Offline')):
            result = self.remote.refresh(self.context['endpointId'])
        self.assertTrue(next(c for c in result['contexts'] if c['origin'] == 'remote')['offline'])
        with self.assertRaisesRegex(ValueError, 'offline'): self.remote.resume(self.context['id'])

    def test_handoff_existing_folder_preserves_source_and_reserves_it(self):
        folder = self.local / 'checkout'; folder.mkdir()
        result = Teleport(self.remote).run(self.context['id'], str(folder))
        local = self.remote.context(result['id'])
        self.assertEqual(local['origin'], 'local'); self.assertEqual(local['project'], str(folder))
        self.assertEqual(Path(local['path']).read_bytes(), self.source.read_bytes())
        source = self.remote.context(self.context['id'])
        self.assertEqual(source['handoff']['state'], 'moved')
        with patch.dict(os.environ, {'HOME': str(self.host)}):
            with self.assertRaisesRegex(ValueError, 'handed off'):
                with context_lock('codex', 'session-remote'): pass
        self.remote.release(source['id'])
        self.assertIsNone(self.remote.context(source['id'])['handoff'])

    def test_handoff_copies_hidden_files_and_internal_links(self):
        (self.project / '.env').write_text('fixture-only')
        (self.project / 'link').symlink_to('source.py')
        destination = self.local / 'copy'; destination.mkdir()
        result = Teleport(self.remote).run(self.context['id'], str(destination), True)
        self.assertEqual((destination / '.env').read_text(), 'fixture-only')
        self.assertTrue((destination / 'link').is_symlink())
        self.assertEqual((destination / 'link').read_text(), (self.project / 'source.py').read_text())
        self.assertEqual(self.remote.context(result['id'])['project'], str(destination))

    def test_failed_transfer_releases_source(self):
        folder = self.local / 'checkout'; folder.mkdir()
        with patch.object(self.remote, 'download', side_effect=ValueError('Transfer failed')):
            with self.assertRaisesRegex(ValueError, 'Transfer failed'):
                Teleport(self.remote).run(self.context['id'], str(folder))
        self.remote.refresh(self.context['endpointId'])
        self.assertIsNone(self.remote.context(self.context['id'])['handoff'])

    def test_source_changes_after_reservation_abort_transfer(self):
        folder = self.local / 'checkout'; folder.mkdir()
        def download(endpoint, operation, destination):
            self.source.write_text(self.source.read_text() + '{"type":"new-event"}\n')
            self.download(endpoint, operation, destination)
        with patch.object(self.remote, 'download', side_effect=download):
            with self.assertRaisesRegex(ValueError, 'changed during handoff'):
                Teleport(self.remote).run(self.context['id'], str(folder))
        self.assertFalse(any(c['origin'] == 'local' for c in self.manager.contexts.values()))
        self.remote.refresh(self.context['endpointId'])
        self.assertIsNone(self.remote.context(self.context['id'])['handoff'])

    def test_existing_local_session_is_not_overwritten(self):
        target = self.local / '.codex/sessions/2026/01/01/existing.jsonl'
        target.parent.mkdir(parents=True); target.write_bytes(self.source.read_bytes())
        folder = self.local / 'checkout'; folder.mkdir()
        with self.assertRaisesRegex(ValueError, 'already exists'):
            Teleport(self.remote).run(self.context['id'], str(folder))
        self.assertEqual(target.read_bytes(), self.source.read_bytes())

    def test_source_identity_change_blocks_resume(self):
        operation = {'operation': 'resume', 'id': self.context['remoteId'], 'machineId': 'wrong-machine'}
        with self.assertRaisesRegex(ValueError, 'different machine'):
            self.request(None, operation)

    def test_finalize_failure_keeps_source_reserved_and_local_copy(self):
        folder = self.local / 'checkout'; folder.mkdir()
        def request(endpoint, operation):
            if operation['operation'] == 'finish': raise ValueError('Offline')
            return self.request(endpoint, operation)
        with patch.object(self.remote, 'request', side_effect=request):
            result = Teleport(self.remote).run(self.context['id'], str(folder))
        self.assertTrue(result['warning'])
        self.assertEqual(self.remote.context(result['id'])['origin'], 'local')
        self.assertEqual(self.remote.context(self.context['id'])['handoff']['state'], 'reserved')

    def test_live_lock_blocks_handoff_and_parallel_resume(self):
        with context_lock('codex', 'lock-test'):
            with self.assertRaisesRegex(ValueError, 'already running'):
                with context_lock('codex', 'lock-test'): pass
            with self.assertRaisesRegex(ValueError, 'already running'):
                reserve_handoff('codex', 'lock-test', 'a'*32, 'destination')
        reserve_handoff('codex', 'lock-test', 'a'*32, 'destination')
        with self.assertRaises(ValueError): update_handoff('codex', 'lock-test', 'b'*32)
        update_handoff('codex', 'lock-test', 'a'*32)
        self.assertIsNone(handoff_status('codex', 'lock-test'))

    def test_workspace_rejects_traversal_and_external_links(self):
        destination = self.local / 'empty'; destination.mkdir()
        archive = self.base / 'unsafe.zip'
        with zipfile.ZipFile(archive, 'w') as z: z.writestr('../outside', 'bad')
        with self.assertRaises(ValueError): extract_workspace(archive, destination)
        (self.project / 'escape').symlink_to('/etc/passwd')
        with self.assertRaisesRegex(ValueError, 'outside'): snapshot_workspace(self.project, self.base / 'links.zip')

    def test_network_scan_only_accepts_attached_subnets(self):
        with patch('backend.network.local_networks', return_value=[{'cidr': '192.168.20.0/30'}]), patch('backend.network.probe_ssh', side_effect=lambda host: {'host': host} if host.endswith('.1') else None):
            self.assertEqual(scan_network('192.168.20.0/30')['hosts'], [{'host': '192.168.20.1'}])
            with self.assertRaises(ValueError): scan_network('8.8.8.0/24')

    def test_gemini_restore_remaps_project_bucket(self):
        import hashlib
        source = self.local / '.gemini/tmp/old/chats/session-demo.json'; source.parent.mkdir(parents=True)
        source.write_text(json.dumps({'sessionId': 'gemini-demo', 'projectHash': 'old', 'messages': []}))
        self.manager.scan(); context = next(c for c in self.manager.contexts.values() if c['provider'] == 'gemini')
        archive = self.base / 'gemini.gptctx'; self.manager.export([context['id']], archive)
        self.manager.import_archive(archive)
        imported = next(c for c in self.manager.contexts.values() if c['origin'] == 'imported')
        folder = self.local / 'newproject'; folder.mkdir()
        target = self.local / '.gemini/tmp'
        self.manager.restore(imported['id'], target, str(folder))
        path = target / hashlib.sha256(str(folder).encode()).hexdigest() / 'chats/session-demo.json'
        self.assertEqual(json.loads(path.read_text())['sessionId'], 'gemini-demo')
        self.assertEqual(json.loads(source.read_text())['projectHash'], 'old')


class GitTests(unittest.TestCase):
    def test_fetch_is_read_only_until_pull_and_dirty_checkout_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            def git(*args):
                return subprocess.run(['git', *map(str, args)], check=True, capture_output=True, text=True).stdout.strip()
            remote = root/'origin'; a=root/'a'; b=root/'b'
            git('init', '--bare', remote); git('clone', remote, a)
            git('-C', a, 'config', 'user.name', 'Fixture'); git('-C', a, 'config', 'user.email', 'fixture@example.invalid')
            (a/'file').write_text('one'); git('-C', a, 'add', 'file'); git('-C', a, 'commit', '-m', 'one'); git('-C', a, 'push', '-u', 'origin', 'HEAD')
            git('clone', remote, b)
            (a/'file').write_text('two'); git('-C', a, 'commit', '-am', 'two'); git('-C', a, 'push')
            self.assertEqual(git_check(b)['behind'], 1); self.assertEqual((b/'file').read_text(), 'one')
            (b/'untracked').write_text('keep')
            with self.assertRaises(ValueError): git_check(b, pull=True)
            (b/'untracked').unlink()
            git_check(b, pull=True); self.assertEqual((b/'file').read_text(), 'two')


if __name__ == '__main__': unittest.main()
