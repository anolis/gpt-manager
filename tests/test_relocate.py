import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.manager import Manager
from backend.relocate import move_files, plan_move, project_folder
from backend.cloud import CloudSync


class RelocateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {}, clear=True); self.env.start()
        self.home = self.root / 'home'; self.home.mkdir()
        self.m = Manager(self.home, self.root / 'manager')
        self.main = self.home / '.claude/projects/project/session.jsonl'
        self.main.parent.mkdir(parents=True)
        self.main.write_text(json.dumps({'sessionId': 'session', 'cwd': str(self.home), 'type': 'user', 'message': {'role': 'user', 'content': 'Relocate this context'}}) + '\n')
        self.child = self.main.with_suffix('') / 'subagents/child.jsonl'; self.child.parent.mkdir(parents=True)
        self.child.write_text('{"type":"fixture"}\n')
        self.m.scan(); self.id = next(iter(self.m.contexts))
        self.dest = self.root / 'new-store'; self.dest.mkdir()

    def tearDown(self): self.env.stop(); self.temp.cleanup()

    def test_project_mapping_persists_without_rewriting_history(self):
        project = self.root / 'new-project'; project.mkdir()
        original = self.main.read_bytes()
        lib = project_folder(self.m, self.id, project)
        self.assertEqual(lib['contexts'][0]['project'], str(project))
        self.assertEqual(self.main.read_bytes(), original)
        restarted = Manager(self.home, self.root / 'manager'); restarted.scan()
        self.assertEqual(restarted.library()['contexts'][0]['project'], str(project))

    def test_move_preserves_bytes_companions_notes_and_recovery_archive(self):
        original = self.main.read_bytes(); companion = self.child.read_bytes()
        self.m.annotate(self.id, notes='Keep these notes', starred=True)
        result = move_files(self.m, self.id, self.dest)
        self.assertEqual(result['count'], 2)
        self.assertFalse(self.main.exists()); self.assertFalse(self.child.exists())
        self.assertEqual((self.dest / 'project/session.jsonl').read_bytes(), original)
        self.assertEqual((self.dest / 'project/session/subagents/child.jsonl').read_bytes(), companion)
        self.assertEqual(len(result['library']['contexts']), 1)
        ctx = result['library']['contexts'][0]
        self.assertEqual(ctx['notes'], 'Keep these notes'); self.assertTrue(ctx['starred'])
        self.assertEqual(ctx['id'], result['id'])
        self.assertEqual(len(self.m.preview(result['backup'])['contexts']), 1)
        restart = Manager(self.home, self.root / 'manager'); restart.scan()
        self.assertIn(result['id'], restart.contexts)

    def test_conflict_prevents_any_move(self):
        target = self.dest / 'project/session.jsonl'; target.parent.mkdir(parents=True); target.write_text('keep me')
        with self.assertRaisesRegex(ValueError, 'conflict'):
            move_files(self.m, self.id, self.dest)
        self.assertTrue(self.main.exists()); self.assertTrue(self.child.exists()); self.assertEqual(target.read_text(), 'keep me')

    def test_symlink_destination_rejected(self):
        outside = self.root / 'outside'; outside.mkdir()
        (self.dest / 'project').symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'escapes|symlink'):
            move_files(self.m, self.id, self.dest)
        self.assertTrue(self.main.exists()); self.assertFalse(list(outside.iterdir()))

    def test_failed_source_removal_rolls_back(self):
        real = Path.unlink
        def failure(path, *args, **kwargs):
            if path == self.child: raise PermissionError('fixture cannot remove')
            return real(path, *args, **kwargs)
        with patch.object(Path, 'unlink', failure):
            with self.assertRaises(PermissionError): move_files(self.m, self.id, self.dest)
        self.assertTrue(self.main.exists()); self.assertTrue(self.child.exists())
        self.assertFalse((self.dest / 'project/session.jsonl').exists())
        self.assertTrue(list((self.m.data / 'move-backups').glob('*.gptctx')))

    def test_changing_source_rejected_before_removal(self):
        real = __import__('backend.relocate', fromlist=['digest']).digest
        reads = 0
        def changing(path):
            nonlocal reads
            if path == self.main:
                reads += 1
                if reads == 2: self.main.write_text(self.main.read_text() + '{"new":"turn"}\n')
            return real(path)
        with patch('backend.relocate.digest', side_effect=changing):
            with self.assertRaisesRegex(ValueError, 'changed'):
                move_files(self.m, self.id, self.dest)
        self.assertTrue(self.main.exists()); self.assertTrue(self.child.exists())
        self.assertFalse((self.dest / 'project/session.jsonl').exists())

    def test_missing_project_refused(self):
        with self.assertRaisesRegex(ValueError, 'existing'):
            project_folder(self.m, self.id, self.root / 'missing')

    def test_cloud_selection_follows_new_identity(self):
        cloud = CloudSync(self.m)
        folder = self.root / 'cloud'; folder.mkdir()
        target = cloud.folder('onedrive', folder)['targets'][0]['id']
        cloud.configure(target, selection=[self.id])
        result = move_files(self.m, self.id, self.dest)
        cloud.remap_context(self.id, result['id'])
        self.assertEqual(cloud.target(target)['selection'], [result['id']])
        cloud.close()

    def test_same_root_and_private_storage_refused(self):
        for destination in (self.home / '.claude/projects', self.m.data):
            with self.assertRaises(ValueError): plan_move(self.m, self.id, destination)


if __name__ == '__main__': unittest.main()
