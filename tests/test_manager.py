import json
import os
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from backend.manager import Manager


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.home = self.base / 'home'
        self.home.mkdir()
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.m = Manager(self.home, self.base / 'data')
        self.codex = self.home / '.codex/sessions/2026/01/01/rollout-test.jsonl'
        self.write_lines(self.codex, [
            {'type': 'session_meta', 'payload': {'id': 'session-1', 'cwd': str(self.home), 'model': 'test-model'}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Build an observatory'}]}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'Let us start with the dome.'}]}},
        ])
        self.claude = self.home / '.claude/projects/test/claude-1.jsonl'
        self.write_lines(self.claude, [{'sessionId': 'claude-1', 'cwd': str(self.home), 'type': 'user', 'message': {'role': 'user', 'content': 'Review telescope controls'}}])
        self.gemini = self.home / '.gemini/tmp/hash/chats/session-1.json'
        self.gemini.parent.mkdir(parents=True)
        self.gemini.write_text(json.dumps({'sessionId': 'gemini-1', 'messages': [{'type': 'user', 'content': 'Map the night sky'}, {'type': 'gemini', 'content': 'Start with Orion.'}]}))
        self.agy = self.home / '.gemini/antigravity-cli/brain/agy-1/.system_generated/logs/transcript.jsonl'
        self.write_lines(self.agy, [{'type': 'USER_INPUT', 'content': 'Plan the observation'}, {'type': 'PLANNER_RESPONSE', 'content': 'We will begin at dusk.'}])
        self.m.scan()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def write_lines(self, path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows))

    def context(self, provider='codex'):
        return next(c for c in self.m.contexts.values() if c['provider'] == provider)

    def bundle(self):
        path = self.base / 'test.gptctx'
        self.m.export(list(self.m.contexts), path)
        return path

    def rewrite(self, archive, mutate, extra=None):
        with zipfile.ZipFile(archive) as z:
            files = {n: z.read(n) for n in z.namelist()}
        m = json.loads(files['manifest.json'])
        mutate(m, files)
        files['manifest.json'] = json.dumps(m).encode()
        if extra:
            files.update(extra)
        out = self.base / 'modified.gptctx'
        with zipfile.ZipFile(out, 'w') as z:
            for k, v in files.items(): z.writestr(k, v)
        return out

    def test_all_providers_and_readable_messages(self):
        self.assertEqual(len(self.m.contexts), 4)
        for provider in ('codex', 'claude', 'gemini', 'antigravity'):
            c = self.context(provider)
            self.assertTrue(self.m.detail(c['id'])['messages'])
        self.assertEqual(self.context()['title'], 'Build an observatory')
        self.assertEqual(self.context('antigravity')['sessionId'], 'agy-1')
        self.assertEqual(self.context('gemini')['sessionId'], 'gemini-1')

    def test_round_trip_and_restore_exact_bytes(self):
        archive = self.bundle()
        other = Manager(self.base / 'other-home', self.base / 'other-manager')
        self.assertEqual(len(other.preview(archive)['contexts']), 4)
        lib = other.import_archive(archive)
        self.assertEqual(len(lib['contexts']), 4)
        restored = self.base / 'restored'; restored.mkdir()
        c = next(c for c in lib['contexts'] if c['provider'] == 'codex')
        self.assertEqual(other.detail(c['id'])['messages'][0]['content'], 'Build an observatory')
        result = other.restore(c['id'], restored)
        self.assertEqual(result['count'], 1)
        self.assertEqual((restored / 'sessions/2026/01/01/rollout-test.jsonl').read_bytes(), self.codex.read_bytes())
        with self.assertRaisesRegex(ValueError, 'conflict'):
            other.restore(c['id'], restored)

    def test_annotations_survive_restart_and_transfer(self):
        c = self.context()
        original = self.codex.read_bytes()
        self.m.annotate(c['id'], title='Telescope', starred=True, tags=['night'], notes='Remember focus')
        m = Manager(self.home, self.base / 'data'); m.scan()
        c = next(c for c in m.library()['contexts'] if c['provider'] == 'codex')
        self.assertTrue(c['starred'])
        archive = self.bundle()
        other = Manager(self.base / 'empty', self.base / 'other')
        c = next(c for c in other.import_archive(archive)['contexts'] if c['provider'] == 'codex')
        self.assertEqual(c['notes'], 'Remember focus')
        self.assertEqual(c['tags'], ['night'])
        self.assertEqual(self.codex.read_bytes(), original)

    def test_checksum_tampering_rejected_before_import(self):
        path = self.rewrite(self.bundle(), lambda m, files: files.__setitem__(m['contexts'][0]['main'], b'tampered'))
        with self.assertRaisesRegex(ValueError, 'mismatch'):
            self.m.import_archive(path)
        self.assertEqual(list((self.m.data / 'imports').glob('*')), [])

    def test_same_size_checksum_tampering(self):
        def corrupt(m, files):
            name = m['contexts'][0]['main']; files[name] = b'x' * len(files[name])
        with self.assertRaisesRegex(ValueError, 'Checksum'):
            self.m.preview(self.rewrite(self.bundle(), corrupt))

    def test_path_traversal_rejected(self):
        path = self.rewrite(self.bundle(), lambda m, f: None, {'../escape': b'bad'})
        with self.assertRaisesRegex(ValueError, 'Unsafe'):
            self.m.import_archive(path)
        self.assertFalse((self.base / 'escape').exists())

    def test_restore_path_traversal_rejected(self):
        def mutate(m, _): m['contexts'][0]['files'][0]['relative'] = '../escape'
        with self.assertRaisesRegex(ValueError, 'Unsafe'):
            self.m.preview(self.rewrite(self.bundle(), mutate))

    def test_undeclared_file_rejected(self):
        path = self.rewrite(self.bundle(), lambda m, f: None, {'secret.txt': b'bad'})
        with self.assertRaisesRegex(ValueError, 'undeclared'):
            self.m.preview(path)

    def test_arbitrary_restore_target_rejected(self):
        def mutate(m, _): m['contexts'][0]['files'][0]['relative'] = '.bashrc'
        with self.assertRaisesRegex(ValueError, 'provider context layout'):
            self.m.preview(self.rewrite(self.bundle(), mutate))

    def test_invalid_tags_rejected(self):
        def mutate(m, _): m['contexts'][0]['context']['tags'] = {'not': 'a list'}
        with self.assertRaisesRegex(ValueError, 'Invalid tags'):
            self.m.preview(self.rewrite(self.bundle(), mutate))

    def test_duplicate_ids_rejected(self):
        def mutate(m, _): m['contexts'][1]['context']['id'] = m['contexts'][0]['context']['id']
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            self.m.preview(self.rewrite(self.bundle(), mutate))

    def test_pagination_preserves_order(self):
        rows = [{'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [{'text': str(i)}]}} for i in range(451)]
        self.write_lines(self.codex, rows); self.m.scan()
        cursor, messages = 0, []
        while cursor is not None:
            page = self.m.detail(self.context()['id'], cursor); cursor = page['next']; messages.extend(page['messages'])
        self.assertEqual([m['content'] for m in messages], [str(i) for i in range(451)])

    def test_partial_last_line_is_tolerated(self):
        with self.codex.open('ab') as f: f.write(b'{partial')
        detail = self.m.detail(self.context()['id'])
        self.assertEqual(len(detail['messages']), 2)
        self.assertEqual(detail['skipped'], 1)

    def test_symlinked_provider_file_excluded(self):
        secret = self.base / 'secret.jsonl'; secret.write_text('private')
        self.codex.parent.joinpath('symlink.jsonl').symlink_to(secret)
        self.m.scan(); self.assertEqual(len(self.m.contexts), 4)

    def test_restore_symlink_parent_rejected(self):
        other = Manager(self.base / 'empty', self.base / 'other')
        lib = other.import_archive(self.bundle())
        c = next(c for c in lib['contexts'] if c['provider'] == 'codex')
        dest = self.base / 'dest'; dest.mkdir(); outside = self.base / 'outside'; outside.mkdir()
        (dest / 'sessions').symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            other.restore(c['id'], dest)
        self.assertEqual(list(outside.iterdir()), [])

    def test_sqlite_wal_contents_backed_up(self):
        dbpath = self.home / '.gemini/antigravity-cli/conversations/agy-1.db'
        dbpath.parent.mkdir(parents=True)
        db = sqlite3.connect(dbpath)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE steps(content text)')
        db.execute('INSERT INTO steps VALUES (?)', ('Latest committed turn',)); db.commit()
        try:
            archive = self.bundle()
            other = Manager(self.base / 'empty', self.base / 'other')
            lib = other.import_archive(archive)
            c = next(c for c in lib['contexts'] if c['provider'] == 'antigravity')
            files = other.files(c['id'])
            f = next(f for f in files if f['relative'].endswith('.db'))
            with sqlite3.connect(f['path']) as snapshot:
                self.assertEqual(snapshot.execute('SELECT content FROM steps').fetchone()[0], 'Latest committed turn')
        finally: db.close()

    def test_export_does_not_include_credentials(self):
        token = self.home / '.gemini/antigravity-cli/antigravity-oauth-token'; token.write_text('secret')
        with zipfile.ZipFile(self.bundle()) as z:
            self.assertFalse(any('token' in x for x in z.namelist()))

    def test_existing_backup_never_overwritten(self):
        archive = self.bundle(); before = archive.read_bytes()
        with self.assertRaisesRegex(ValueError, 'never overwritten'):
            self.m.export(list(self.m.contexts), archive)
        self.assertEqual(archive.read_bytes(), before)

    def test_reexport_imported_context(self):
        other = Manager(self.base / 'empty', self.base / 'other')
        lib = other.import_archive(self.bundle())
        output = self.base / 'again.gptctx'
        other.export([c['id'] for c in lib['contexts']], output)
        self.assertEqual(len(other.preview(output)['contexts']), 4)


if __name__ == '__main__': unittest.main()
