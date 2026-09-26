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
