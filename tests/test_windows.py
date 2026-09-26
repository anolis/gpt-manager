"""Cross-platform data/locking checks, also run on a native Windows runner."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from backend.manager import Manager
from backend.session_lock import context_lock, reserve_handoff, update_handoff, handoff_status
from backend.remote import ssh_arguments
from backend.network import local_networks


class PlatformTests(unittest.TestCase):
    def test_lock_and_handoff_block_other_processes(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {'HOME': temporary, 'USERPROFILE': temporary}):
            code = "from backend.session_lock import context_lock\nwith context_lock('codex','fixture'): pass"
            with context_lock('codex', 'fixture'):
                result = subprocess.run([sys.executable, '-c', code], capture_output=True)
                self.assertNotEqual(result.returncode, 0)
            token = 'a' * 32
            reserve_handoff('codex', 'fixture', token, 'Other computer')
            self.assertEqual(handoff_status('codex', 'fixture')['state'], 'reserved')
            with self.assertRaises(ValueError):
                with context_lock('codex', 'fixture'): pass
            update_handoff('codex', 'fixture', token, finish=True)
            update_handoff('codex', 'fixture', token)
            with context_lock('codex', 'fixture'): pass
    def test_archive_roundtrip_with_native_project_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); home = root / 'home'; source = home / '.codex/sessions/chat.jsonl'; source.parent.mkdir(parents=True)
            source.write_text(json.dumps({'type':'session_meta','payload':{'id':'fixture','cwd':str(root / 'project space')}})+'\n')
            with patch.dict(os.environ, {'CODEX_HOME':str(home / '.codex')}):
                manager = Manager(home, root / 'data'); manager.scan()
                context = next(iter(manager.contexts.values()))
                self.assertEqual(context['project'], str(root / 'project space'))
                bundle = root / 'fixture.gptctx'; manager.export([context['id']], str(bundle))
                target = Manager(root / 'other-home', root / 'other-data'); target.import_archive(str(bundle))
                restored = next(c for c in target.contexts.values() if c['origin']=='imported')
                (root / 'restored').mkdir()
                target.restore(restored['id'], str(root / 'restored'))
                self.assertEqual((root / 'restored/sessions/chat.jsonl').read_bytes(), source.read_bytes())
    def test_ssh_payload_fits_windows_process_limit(self):
        args = ssh_arguments('fixture', {'operation':'scan'})
        self.assertLess(len(subprocess.list2cmdline(['ssh', *args])), 32000)
    def test_windows_network_discovery(self):
        response = subprocess.CompletedProcess([], 0, json.dumps({'InterfaceAlias':'Wi-Fi', 'IPAddress':'192.168.1.42', 'PrefixLength':16}), '')
        with patch('backend.network.platform.system', return_value='Windows'), patch('backend.network.subprocess.run', return_value=response), patch('backend.network.subprocess.CREATE_NO_WINDOW', 0, create=True):
            self.assertEqual(local_networks(), [{'cidr':'192.168.1.0/24','interface':'Wi-Fi','localAddress':'192.168.1.42'}])
