import io
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

if sys.platform == 'linux':
    from tools.linux_install import Installer, NODE_VERSION

SOURCE = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == 'linux', 'Linux installer')
class LinuxInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / 'home with spaces $dollar %percent'
        self.installer = Installer(self.home, self.home / '.local/share')
        self.home.mkdir()

    def tearDown(self): self.temp.cleanup()

    def dependencies(self, stage):
        binary = stage / 'node_modules/electron/dist/electron'
        binary.parent.mkdir(parents=True)
        binary.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n'); binary.chmod(0o755)

    def install(self, fail=False):
        with patch.object(self.installer, 'node_runtime'), patch.object(self.installer, 'dependencies', side_effect=RuntimeError('network failed') if fail else self.dependencies), patch.object(self.installer, 'verify'), patch.object(self.installer, 'sandbox'), patch.object(self.installer, 'refresh_desktop'), patch('builtins.print'):
            self.installer.install(SOURCE)

    def test_install_update_and_launcher_work_with_spaces_and_special_characters(self):
        self.install()
        first = (self.installer.root/'current').resolve()
        for path in (self.installer.desktop, *self.installer.icons): self.assertTrue(path.is_file())
        if shutil.which('desktop-file-validate'):
            subprocess.run(['desktop-file-validate', str(self.installer.desktop)], check=True)
        self.assertNotIn('--no-sandbox', (self.installer.bin/'gpt-manager').read_text())
        self.install()
        second = (self.installer.root/'current').resolve()
        self.assertNotEqual(first, second); self.assertTrue(first.is_dir())
        result = subprocess.run([str(self.installer.bin/'gpt-manager'), 'argument with spaces'], capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.splitlines(), [str(second), '--class=gpt-manager', 'argument with spaces'])

    def test_failed_update_preserves_working_install_and_cleans_stage(self):
        self.install(); first = (self.installer.root/'current').resolve()
        with self.assertRaises(RuntimeError): self.install(fail=True)
        self.assertEqual((self.installer.root/'current').resolve(), first)
        self.assertEqual(list((self.installer.root/'versions').iterdir()), [first])

    def test_uninstall_keeps_contexts_settings_and_unrelated_launchers(self):
        self.install()
        contexts = self.home/'.codex/sessions/keep.jsonl'; contexts.parent.mkdir(parents=True); contexts.write_text('keep')
        settings = self.home/'.config/gpt-manager/settings.json'; settings.parent.mkdir(parents=True); settings.write_text('keep')
        custom = self.installer.bin/'gpt-manager'; custom.write_text('#!/bin/sh\necho replacement\n')
        with patch.object(self.installer, 'refresh_desktop'), patch('builtins.print'):
            self.installer.uninstall()
        self.assertFalse(self.installer.root.exists()); self.assertFalse(self.installer.desktop.exists())
        self.assertTrue(custom.exists()); self.assertEqual(contexts.read_text(), 'keep'); self.assertEqual(settings.read_text(), 'keep')
        self.assertTrue(all(not icon.exists() for icon in self.installer.icons))

    def test_refuses_unrecognized_install_folder_or_foreign_launcher(self):
        self.installer.root.mkdir(parents=True)
        (self.installer.root/'keep').write_text('keep')
        with self.assertRaises(ValueError): self.install()
        self.assertEqual((self.installer.root/'keep').read_text(), 'keep')

    def test_rejects_install_root_symlink(self):
        self.installer.root.parent.mkdir(parents=True)
        other = self.home/'other'; other.mkdir()
        self.installer.root.symlink_to(other, target_is_directory=True)
        with self.assertRaises(ValueError): self.installer.uninstall()
        self.assertTrue(other.exists())

    def test_tampered_node_download_is_never_extracted(self):
        class Response(io.BytesIO): headers = {'Content-Length': '5'}
        stage = self.home/'stage'; stage.mkdir()
        with patch('tools.linux_install.urlopen', return_value=Response(b'wrong')), patch('tools.linux_install.platform.machine', return_value='x86_64'), patch('builtins.print'):
            with self.assertRaisesRegex(ValueError, 'checksum'): self.installer.node_runtime(stage)
        self.assertFalse((stage/'node').exists())

    def test_verified_node_cache_is_reused_and_unsafe_archives_are_rejected(self):
        name = f'node-{NODE_VERSION}-linux-x64'
        self.installer.root.mkdir(parents=True)
        def archive(member_name):
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode='w:gz') as bundle:
                member = tarfile.TarInfo(member_name); member.size = 4
                bundle.addfile(member, io.BytesIO(b'node'))
            return buffer.getvalue()
        class Response(io.BytesIO): headers = {}
        payload = archive(name+'/bin/node')
        with patch('tools.linux_install.platform.machine', return_value='x86_64'), patch.dict('tools.linux_install.NODE_HASHES', {'x64':hashlib.sha256(payload).hexdigest()}), patch('tools.linux_install.urlopen', return_value=Response(payload)) as fetch, patch('builtins.print'):
            for index in range(2):
                stage = self.home / str(index); stage.mkdir(); self.installer.node_runtime(stage)
                self.assertEqual((stage/'node/bin/node').read_bytes(), b'node')
            self.assertEqual(fetch.call_count, 1)
        payload = archive('../outside')
        with patch('tools.linux_install.platform.machine', return_value='x86_64'), patch.dict('tools.linux_install.NODE_HASHES', {'x64':hashlib.sha256(payload).hexdigest()}), patch('tools.linux_install.urlopen', return_value=Response(payload)), patch('builtins.print'):
            stage = self.home/'unsafe'; stage.mkdir()
            with self.assertRaisesRegex(ValueError, 'Unsafe'): self.installer.node_runtime(stage)
            self.assertFalse((self.home/'outside').exists())

    def test_electron_install_is_explicit_when_npm_blocks_lifecycle_scripts(self):
        stage = self.home/'stage'; stage.mkdir()
        with patch('tools.linux_install.run') as execute, patch.dict(os.environ, {'npm_config_ignore_scripts':'true', 'ELECTRON_SKIP_BINARY_DOWNLOAD':'1'}):
            self.installer.dependencies(stage)
        self.assertEqual(execute.call_count, 2)
        self.assertIn('--ignore-scripts', execute.call_args_list[0].args[0])
        self.assertEqual(execute.call_args_list[1].args[0][1], stage/'node_modules/electron/install.js')
        self.assertNotIn('ELECTRON_SKIP_BINARY_DOWNLOAD', execute.call_args_list[1].kwargs['env'])

    def test_dependency_bootstrap_selects_debian_time64_packages(self):
        commands = self.home/'commands'; commands.mkdir()
        for name, script in {
            'sudo': '#!/bin/sh\nprintf "%s " "$@"\nprintf "\\n"\n',
            'apt-get': '#!/bin/sh\nexit 0\n',
            'apt-cache': '#!/bin/sh\necho "Package: fixture"\n',
        }.items():
            file = commands/name; file.write_text(script); file.chmod(0o755)
        (commands/'grep').symlink_to(shutil.which('grep'))
        result = subprocess.run(['/bin/bash', '-c', 'source "$1"; install_dependencies', 'test', str(SOURCE/'install-linux.sh')], env={**os.environ, 'PATH':str(commands)}, capture_output=True, text=True, check=True)
        self.assertIn('apt-get update', result.stdout)
        for name in ('libgtk-3-0t64', 'libasound2t64', 'libatk-bridge2.0-0t64', 'python3'):
            self.assertIn(name, result.stdout)
