import json
import os
import shutil
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from backend.cloud import CloudSync, Connector, REMOTE_DIR
from backend.manager import Manager


class FakeConnector:
    def __init__(self): self.calls = []; self.closed = False
    def call(self, method, params):
        self.calls.append((method, params))
        if method == 'config/create':
            if params['type'] == 'iclouddrive':
                return {'State': 'two-factor', 'Option': {'Name': 'config_2fa', 'Help': 'Enter the code from your trusted device', 'Required': True, 'IsPassword': True, 'DefaultStr': 'SECRET-MUST-NOT-BE-RETURNED'}}
            return {'State': 'oauth', 'Option': {'Name': 'config_is_local'}}
        if method == 'config/update':
            if params['opt']['state'] == 'oauth':
                return {'State': 'choose-drive', 'Option': {'Name': 'config_driveid', 'Examples': [{'Value': 'drive-1', 'Help': 'Personal OneDrive'}]}}
            return {'State': ''}
        return {}
    def close(self): self.closed = True


class CloudTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {}, clear=True); self.env.start()
        self.home = self.root / 'home'; self.home.mkdir()
        self.m = Manager(self.home, self.root / 'manager-a')
        self.source = self.home / '.codex/sessions/session.jsonl'
        self.source.parent.mkdir(parents=True)
        self.rows = [{'type': 'session_meta', 'payload': {'id': 'test-context', 'cwd': str(self.home)}}, {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [{'text': 'Portable astronomy project'}]}}]
        self.source.write_text(''.join(json.dumps(r) + '\n' for r in self.rows))
        self.m.scan(); self.id = next(iter(self.m.contexts))
        self.connector = FakeConnector(); self.cloud = CloudSync(self.m, connector=self.connector)
        self.remote = self.root / 'drive'; self.remote.mkdir()
        self.target = self.cloud.folder('google', self.remote)['targets'][0]['id']
        self.other = Manager(self.root / 'empty-home', self.root / 'manager-b'); self.other.scan()
        self.b = CloudSync(self.other, connector=FakeConnector())
        self.btarget = self.b.folder('google', self.remote)['targets'][0]['id']

    def tearDown(self):
        self.cloud.close(); self.b.close(); self.env.stop(); self.temp.cleanup()

    def finish(self, cloud):
        for _ in range(300):
            status = cloud.status()
            if status['job']['status'] != 'running': return status
            time.sleep(.01)
        self.fail('Cloud operation did not finish')

    def sync(self, cloud, target):
        cloud.sync(target); status = self.finish(cloud)
        self.assertEqual(status['job']['status'], 'complete', status)
        return status['job']['result']

    def test_two_managers_round_trip_and_no_duplicate_sync(self):
        self.cloud.configure(self.target, selection=[self.id])
        self.assertEqual(self.sync(self.cloud, self.target)['exported'], 1)
        self.assertEqual(self.sync(self.b, self.btarget)['imported'], 1)
        self.assertEqual(self.sync(self.cloud, self.target)['exported'], 0)
        self.assertEqual(self.sync(self.b, self.btarget)['imported'], 0)
        item = next(iter(self.other.contexts.values()))
        self.assertEqual(self.other.detail(item['id'])['messages'][0]['content'], 'Portable astronomy project')
        self.assertEqual(self.source.read_text(), ''.join(json.dumps(r) + '\n' for r in self.rows))

    def test_connection_is_receive_only_by_default(self):
        self.assertEqual(self.sync(self.cloud, self.target)['exported'], 0)
        self.assertEqual(list((self.remote / REMOTE_DIR).iterdir()), [])
        self.assertFalse(self.cloud.target(self.target)['auto'])

    def test_changes_create_new_immutable_snapshots(self):
        self.cloud.configure(self.target, selection=[self.id]); self.sync(self.cloud, self.target)
        old = next((self.remote / REMOTE_DIR).glob('*.gptctx')); original = old.read_bytes()
        self.source.write_text(self.source.read_text() + json.dumps(self.rows[1]) + '\n')
        self.assertEqual(self.sync(self.cloud, self.target)['exported'], 1)
        self.assertEqual(len(list((self.remote / REMOTE_DIR).glob('*.gptctx'))), 2)
        self.assertEqual(old.read_bytes(), original)

    def test_annotations_trigger_new_snapshot(self):
        self.cloud.configure(self.target, selection=[self.id]); self.sync(self.cloud, self.target)
        self.m.annotate(self.id, notes='New observatory coordinates')
        self.assertEqual(self.sync(self.cloud, self.target)['exported'], 1)

    def test_all_local_excludes_archived_and_imported(self):
        self.cloud.configure(self.target, allLocal=True)
        self.m.annotate(self.id, archived=True)
        self.assertEqual(self.sync(self.cloud, self.target)['exported'], 0)
        self.m.annotate(self.id, archived=False); self.sync(self.cloud, self.target); self.sync(self.b, self.btarget)
        self.b.configure(self.btarget, allLocal=True)
        self.assertEqual(self.sync(self.b, self.btarget)['exported'], 0)

    def test_malformed_archive_not_imported_or_marked_received(self):
        folder = self.remote / REMOTE_DIR; folder.mkdir(parents=True)
        name = uuid.uuid4().hex + '-' + uuid.uuid4().hex + '.gptctx'
        (folder / name).write_text('invalid archive')
        result = self.sync(self.b, self.btarget)
        self.assertTrue(result['warnings']); self.assertFalse(self.other.contexts); self.assertFalse(self.b.state['received'])

    def test_partial_and_unrecognized_files_ignored(self):
        folder = self.remote / REMOTE_DIR; folder.mkdir(parents=True)
        for name in ('notes.txt', 'x.gptctx', uuid.uuid4().hex + '-' + uuid.uuid4().hex + '.gptctx.partial'):
            (folder / name).write_text('ignore')
        self.assertEqual(self.sync(self.b, self.btarget)['imported'], 0)

    def test_missing_mount_is_not_recreated(self):
        self.remote.rename(self.root / 'offline')
        self.cloud.sync(self.target); state = self.finish(self.cloud)
        self.assertEqual(state['job']['status'], 'error')
        self.assertFalse(self.remote.exists())

    def test_symlink_destination_refused(self):
        outside = self.root / 'outside'; outside.mkdir()
        (self.remote / 'GPT Manager').symlink_to(outside)
        self.cloud.sync(self.target)
        self.assertEqual(self.finish(self.cloud)['job']['status'], 'error')
        self.assertEqual(list(outside.iterdir()), [])

    def test_disconnect_keeps_archives_and_imports(self):
        self.cloud.configure(self.target, selection=[self.id]); self.sync(self.cloud, self.target); self.sync(self.b, self.btarget)
        self.cloud.disconnect(self.target)
        self.assertTrue(list((self.remote / REMOTE_DIR).glob('*.gptctx')))
        self.assertTrue(self.other.contexts)
        self.assertFalse(self.cloud.state['targets'])

    def test_automatic_sync_is_opt_in_and_throttled(self):
        self.assertIsNone(self.cloud.tick()['job'])
        self.cloud.configure(self.target, selection=[self.id], auto=True)
        self.cloud.tick(); self.finish(self.cloud)
        job_id = self.cloud.status()['job']['id']
        self.assertEqual(self.cloud.tick()['job']['id'], job_id)

    def test_google_registration_is_maintainer_configuration(self):
        with self.assertRaisesRegex(ValueError, 'maintainer'):
            self.cloud.connect('google')
        with self.assertRaisesRegex(ValueError, 'Desktop'):
            self.cloud.configure_google({'web': {'client_id': 'wrong'}})
        result = self.cloud.configure_google({'installed': {'client_id': 'fixture.apps.googleusercontent.com', 'client_secret': 'fixture-secret', 'token_uri': 'https://malicious.invalid'}})
        self.assertTrue(result['googleReady'])
        self.assertNotIn('token_uri', self.cloud.oauth_config()['google'])
        self.assertNotIn('fixture-secret', json.dumps(result))

    def test_onedrive_oauth_then_drive_picker(self):
        self.cloud.connect('onedrive'); result = self.finish(self.cloud)
        self.assertEqual(result['job']['result']['question']['name'], 'config_driveid')
        self.assertEqual(self.connector.calls[1][1]['opt']['result'], 'true')
        self.cloud.answer('drive-1'); result = self.finish(self.cloud)
        self.assertTrue(result['job']['result']['connected'])
        account = next(t for t in result['targets'] if t['mode'] == 'account')
        self.assertNotIn('state', account)
        self.assertEqual(self.connector.calls[-1][0], 'operations/mkdir')

    def test_icloud_password_not_exposed_in_status(self):
        self.cloud.connect('icloud', 'user@example.test', 'private-fixture-password')
        result = self.finish(self.cloud)
        self.assertNotIn('private-fixture-password', json.dumps(result))
        self.assertNotIn('SECRET-MUST-NOT-BE-RETURNED', json.dumps(result))
        question = result['job']['result']['question']
        self.assertTrue(question['password']); self.assertEqual(question['default'], '')
        self.cloud.answer('123456'); self.assertTrue(self.finish(self.cloud)['job']['result']['connected'])

    def test_verified_installer_handles_signed_checksum_file(self):
        import zipfile, hashlib
        package = self.root / 'package.zip'
        with zipfile.ZipFile(package, 'w') as z:
            z.writestr('rclone-v1.75.1-linux-amd64/rclone', 'fixture-executable')
            z.writestr('rclone-v1.75.1-linux-amd64/README.txt', 'MIT license fixture')
        checksum = hashlib.sha256(package.read_bytes()).hexdigest()
        def download(url, target, limit, progress=None):
            if url.endswith('SHA256SUMS'):
                target.write_text('-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA1\n\n' + checksum + '  rclone-v1.75.1-linux-amd64.zip\n')
            else: shutil.copyfile(package, target)
        connector = Connector(self.root / 'installer')
        with patch.object(connector, 'download', side_effect=download), patch('platform.system', return_value='Linux'), patch('platform.machine', return_value='x86_64'):
            self.assertEqual(connector.binary().read_text(), 'fixture-executable')

    def test_download_reports_bytes_with_and_without_content_length(self):
        import io
        data = b'x' * (1024 * 1024 + 37)
        for length in (str(len(data)), None):
            response = io.BytesIO(data)
            response.headers = {'Content-Length': length} if length else {}
            updates = []
            destination = self.root / ('download-' + str(length))
            with patch('urllib.request.urlopen', return_value=response):
                Connector.download('https://example.invalid/connector', destination, len(data),
                                   lambda received, total: updates.append((received, total)))
            self.assertEqual(destination.read_bytes(), data)
            self.assertEqual([item[0] for item in updates], [0, 1024 * 1024, len(data)])
            self.assertEqual(updates[-1][1], len(data) if length else None)

    def test_setup_stage_clears_download_progress(self):
        self.cloud.job = {'status': 'running'}
        self.cloud._message('Downloading', {'received': 10, 'total': 20})
        self.assertEqual(self.cloud.status()['job']['progress']['received'], 10)
        self.cloud._message('Verifying')
        self.assertIsNone(self.cloud.status()['job']['progress'])

    def test_installer_rejects_tampered_binary(self):
        def download(url, target, limit, progress=None):
            target.write_text('wrong bytes' if url.endswith('.zip') else '0' * 64 + '  rclone-v1.75.1-linux-amd64.zip\n')
        connector = Connector(self.root / 'bad-installer')
        with patch.object(connector, 'download', side_effect=download), patch('platform.system', return_value='Linux'), patch('platform.machine', return_value='x86_64'):
            with self.assertRaisesRegex(ValueError, 'checksum'):
                connector.binary()




class ConnectorEndpointTests(unittest.TestCase):
    def test_oauth_callback_does_not_replace_authenticated_control_endpoint(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from unittest.mock import Mock
        requests = []
        class Control(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append((self.path, self.headers.get('Authorization')))
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers(); self.wfile.write(b'{}')
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Control)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with tempfile.TemporaryDirectory() as temp:
                connector = Connector(temp)
                process = Mock(); process.poll.return_value = None; connector.process = process
                endpoint = 'http://127.0.0.1:' + str(server.server_port)
                connector._service_announcement(process, connector.ready, 'NOTICE: Serving remote control on ' + endpoint + '/')
                for line in (
                    'NOTICE: Make sure your Redirect URL is set to "http://127.0.0.1:53682/" in your custom config.',
                    'NOTICE: If your browser doesn\'t open automatically go to the following link: http://127.0.0.1:53682/auth?state=fixture-private-state',
                    'NOTICE: Waiting for code...', 'NOTICE: Got code',
                    'NOTICE: Serving remote control on http://127.0.0.1:1/'):
                    connector._service_announcement(process, connector.ready, line)
                self.assertEqual(connector.url, endpoint)
                self.assertTrue(connector.ready.is_set())
                self.assertEqual(connector.call('operations/mkdir', {'fs':'fixture:', 'remote':'GPT Manager/v1'}), {})
                self.assertEqual(len(requests), 1)
                self.assertEqual(requests[0][0], '/operations/mkdir')
                self.assertTrue(requests[0][1].startswith('Basic '))
                self.assertNotIn('fixture-private-state', json.dumps(requests))
        finally:
            server.shutdown(); server.server_close(); thread.join(2)

    def test_only_current_process_service_announcement_can_signal_readiness(self):
        import threading
        with tempfile.TemporaryDirectory() as temp:
            connector = Connector(temp); old, current = object(), object(); connector.process = current
            old_ready, ready = threading.Event(), threading.Event()
            connector._service_announcement(old, old_ready, 'Serving remote control on http://127.0.0.1:1234/')
            for line in ('redirect http://127.0.0.1:53682/', 'Serving remote control on http://evil.test:1234/', 'Serving remote control on http://127.0.0.1:99999/', 'Serving remote control on http://127.0.0.1:1234.evil.test/'):
                connector._service_announcement(current, ready, line)
            self.assertIsNone(connector.url); self.assertFalse(ready.is_set()); self.assertFalse(old_ready.is_set())
            connector._service_announcement(current, ready, 'NOTICE: Serving remote control on http://127.0.0.1:1234/')
            self.assertTrue(ready.is_set()); self.assertEqual(connector.url, 'http://127.0.0.1:1234')


if __name__ == '__main__': unittest.main()
