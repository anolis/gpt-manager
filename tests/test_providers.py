import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from backend.providers import ProviderSetup


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.setup = ProviderSetup(self.tmp.name)
    def tearDown(self):
        self.setup.close()
        if self.setup.thread: self.setup.thread.join(10)
        self.tmp.cleanup()
    def wait(self):
        self.setup.thread.join(10)
        self.assertFalse(self.setup.thread.is_alive())
        return self.setup.status()['job']
    def package(self, folder, provider='codex', entry='bin/main.js'):
        package = folder / 'node_modules/@openai/codex'
        package.mkdir(parents=True)
        (package / 'package.json').write_text(json.dumps({'bin': {provider: entry}, 'version': '1.2.3'}))
        binary = package / 'bin/main.js'; binary.parent.mkdir(); binary.write_text('')
        return binary
    def test_unknown_provider_cannot_install_arbitrary_package(self):
        for value in ('--help', '@evil/package', 'codex;whoami', '../codex'):
            with self.assertRaises(ValueError): self.setup.install(value)
    def test_runtime_paths_and_checksums_cover_windows(self):
        with patch('backend.providers.platform.system', return_value='Windows'), patch('backend.providers.platform.machine', return_value='AMD64'):
            key, folder, node, npm = self.setup.runtime()
            self.assertEqual(key, 'win-x64'); self.assertEqual(node.name, 'node.exe')
            self.assertEqual(npm.relative_to(folder).as_posix(), 'node_modules/npm/bin/npm-cli.js')
    def test_js_entry_uses_managed_node_without_shell_shim(self):
        folder = self.setup.root / 'fixture'; binary = self.package(folder)
        result = self.setup.package_command(folder, 'codex')
        self.assertEqual(result[1], str(binary.resolve()))
        self.assertEqual(Path(result[0]).name.lower(), 'node.exe' if os.name == 'nt' else 'node')
    def test_package_entry_cannot_escape_install(self):
        folder = self.setup.root / 'fixture'; self.package(folder, entry='../../../../outside.js')
        with self.assertRaises(ValueError): self.setup.package_command(folder, 'codex')
    def test_tampered_runtime_is_not_extracted_or_executed(self):
        class Response(io.BytesIO): headers = {'Content-Length': '5'}
        with patch('backend.providers.urlopen', return_value=Response(b'wrong')):
            self.setup.job = {}
            with self.assertRaisesRegex(ValueError, 'checksum'): self.setup._download_runtime()
        self.assertFalse(self.setup.runtime()[2].exists())
    def test_failed_update_preserves_previous_install(self):
        record = self.setup.root / 'codex.json'; original = '{"folder":"previous","version":"1"}'; record.write_text(original)
        with patch.object(self.setup, '_download_runtime'), patch.object(self.setup, '_run', side_effect=ValueError('fixture failure')):
            self.setup.install('codex'); self.assertEqual(self.wait()['status'], 'error')
        self.assertEqual(record.read_text(), original)
        self.assertFalse(list(self.setup.root.glob('codex-*')))
    def test_success_publishes_only_after_version_check(self):
        calls = []
        def run(args, cwd, timeout=600):
            calls.append(args)
            if 'install' in args: self.package(cwd)
            return '1.2.3'
        with patch.object(self.setup, '_download_runtime'), patch.object(self.setup, '_run', side_effect=run):
            self.setup.install('codex'); self.assertEqual(self.wait()['status'], 'complete')
        self.assertIn('@openai/codex@latest', calls[0]); self.assertIn('--version', calls[1])
        self.assertTrue(self.setup.status()['providers'][0]['managed'])
        self.assertTrue(Path(self.setup.command('codex')[1]).exists())
    def test_process_cancel_terminates_installer(self):
        import threading
        errors = []
        def run():
            try: self.setup._run([sys.executable, '-c', 'import time; time.sleep(60)'], Path(self.tmp.name))
            except InterruptedError: errors.append('canceled')
        thread = threading.Thread(target=run); thread.start()
        for _ in range(100):
            if self.setup.process: break
            time.sleep(.02)
        process = self.setup.process
        self.assertIsNotNone(process)
        self.setup.cancel(); thread.join(8)
        self.assertFalse(thread.is_alive()); self.assertEqual(errors, ['canceled']); self.assertIsNotNone(process.poll())

    def test_existing_cli_preferred_unless_user_selects_managed(self):
        with patch.object(self.setup, 'existing_command', return_value=['/existing/codex']), patch.object(self.setup, 'managed_command', return_value=['/managed/codex']):
            self.assertEqual(self.setup.command('codex'), ['/existing/codex'])
            self.setup.prefer('codex', 'managed')
            self.assertEqual(self.setup.command('codex'), ['/managed/codex'])
            self.setup.prefer('codex', 'existing')
            self.assertEqual(self.setup.command('codex'), ['/existing/codex'])
    def test_managed_is_fallback_if_no_existing_cli(self):
        with patch.object(self.setup, 'existing_command', return_value=None), patch.object(self.setup, 'managed_command', return_value=['/managed/codex']):
            self.assertEqual(self.setup.command('codex'), ['/managed/codex'])

    def test_uninstall_preserves_external_cli_credentials_and_other_providers(self):
        managed = self.setup.root / ('codex-' + 'a'*32); self.package(managed)
        old = self.setup.root / ('codex-' + 'b'*32); old.mkdir()
        other = self.setup.root / ('claude-' + 'c'*32); other.mkdir()
        (self.setup.root/'codex.json').write_text(json.dumps({'folder':managed.name,'version':'1.2.3'}))
        credentials = Path(self.tmp.name)/'auth.json'; credentials.write_text('private fixture')
        with patch.object(self.setup,'existing_command',return_value=['existing-cli']):
            self.setup.prefer('codex','managed'); self.setup.uninstall('codex')
            self.assertEqual(self.setup.command('codex'),['existing-cli'])
        self.assertFalse(managed.exists()); self.assertFalse(old.exists()); self.assertTrue(other.exists())
        self.assertEqual(credentials.read_text(),'private fixture')
        self.assertFalse((self.setup.root/'codex.json').exists())
    def test_uninstall_cannot_follow_manifest_outside_managed_root(self):
        outside = Path(self.tmp.name)/'outside'; outside.mkdir(); (outside/'keep').write_text('keep')
        (self.setup.root/'codex.json').write_text(json.dumps({'folder':'../outside'}))
        self.setup.uninstall('codex'); self.assertTrue((outside/'keep').exists())
    def test_uninstall_refuses_during_install_or_auth_check(self):
        self.setup.job = {'status':'running'}
        with self.assertRaises(ValueError): self.setup.uninstall('codex')
        self.setup.job = None; self.setup.auth_busy = True
        with self.assertRaises(ValueError): self.setup.uninstall('codex')
    def test_auth_checks_report_state_without_identity_or_token_output(self):
        script = Path(self.tmp.name)/'auth.py'
        script.write_text("import sys,json\nif sys.argv[1]=='login': print('Logged in using ChatGPT',file=sys.stderr)\nelse: print(json.dumps({'loggedIn':True,'email':'PRIVATE','token':'SECRET'}))\n")
        for provider in ('codex','claude'):
            result = self.setup._auth_check(provider,[sys.executable,str(script)])
            self.assertEqual(result['state'],'signed_in'); self.assertNotIn('PRIVATE',json.dumps(result)); self.assertNotIn('SECRET',json.dumps(result))
        script.write_text("import sys,json\nif sys.argv[1]=='login': print('Not logged in',file=sys.stderr)\nelse: print(json.dumps({'loggedIn':False}))\nsys.exit(1)\n")
        for provider in ('codex','claude'):
            self.assertEqual(self.setup._auth_check(provider,[sys.executable,str(script)])['state'],'signed_out')
    def test_gemini_saved_credentials_are_not_claimed_as_verified(self):
        home = Path(self.tmp.name)/'gemini-home'; (home/'.gemini').mkdir(parents=True)
        (home/'.gemini/oauth_creds.json').write_text(json.dumps({'refresh_token':'PRIVATE'}))
        with patch.dict(os.environ, {'GEMINI_CLI_HOME':str(home),'GEMINI_API_KEY':'','GOOGLE_API_KEY':''}):
            result = self.setup._auth_check('gemini',['unused'])
        self.assertEqual(result['state'],'configured'); self.assertIn('not verified',result['label']); self.assertNotIn('PRIVATE',json.dumps(result))
