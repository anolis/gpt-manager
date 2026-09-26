"""Local context discovery and lossless, checked archive transport. Standard library only."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse, unquote

VERSION = 1
MAX_TOTAL = 16 * 1024**3
MAX_FILES = 50000
PROVIDERS = {'codex': 'Codex', 'claude': 'Claude', 'gemini': 'Gemini CLI', 'antigravity': 'Antigravity'}


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for part in iter(lambda: f.read(1024 * 1024), b''):
            h.update(part)
    return h.hexdigest()


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    tmp.replace(path)


def read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def text_content(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return '\n'.join(filter(None, (text_content(x) for x in value)))
    if isinstance(value, dict):
        for key in ('text', 'content', 'output', 'result'):
            if key in value:
                return text_content(value[key])
    return ''


def message(record, provider):
    if not isinstance(record, dict):
        return None
    ts = record.get('timestamp', record.get('created_at', ''))
    role, content, kind = '', '', ''
    if provider == 'codex':
        if record.get('type') != 'response_item':
            return None
        p = record.get('payload', {})
        kind = p.get('type', '')
        if kind == 'message':
            role, content = p.get('role', 'system'), text_content(p.get('content'))
        elif kind in ('function_call', 'custom_tool_call'):
            role, content = 'tool', p.get('name', '') + '\n' + text_content(p.get('arguments', p.get('input', '')))
        elif kind in ('function_call_output', 'custom_tool_call_output'):
            role, content = 'tool', text_content(p.get('output'))
    elif provider == 'claude':
        p = record.get('message', {})
        if isinstance(p, dict):
            role, content = p.get('role', ''), text_content(p.get('content'))
            for block in p.get('content', []) if isinstance(p.get('content'), list) else []:
                if isinstance(block, dict) and block.get('type') == 'tool_use':
                    content += '\n' + block.get('name', 'tool') + '\n' + json.dumps(block.get('input', {}), ensure_ascii=False)
                if isinstance(block, dict) and block.get('type') == 'tool_result':
                    role = 'tool'
        kind = record.get('type', '')
    elif provider == 'gemini':
        role = {'gemini': 'assistant', 'user': 'user'}.get(record.get('type'), record.get('role', ''))
        content = text_content(record.get('content', record.get('text', '')))
        kind = record.get('type', '')
    else:
        kind = record.get('type', '')
        role = {'USER_INPUT': 'user', 'PLANNER_RESPONSE': 'assistant'}.get(kind, 'tool')
        content = text_content(record.get('content'))
        if not content and record.get('thinking'):
            role, content = 'reasoning', text_content(record['thinking'])
    if not role or not content:
        return None
    # Keep one pathological tool output from locking up the renderer.
    truncated = len(content) > 100000
    return {'role': role, 'content': content[:100000], 'timestamp': ts, 'kind': kind, 'truncated': truncated}


def sample_records(path):
    """Only read a bounded prefix for the library; full files can be gigabytes."""
    if path.suffix == '.json':
        if path.stat().st_size > 32 * 1024**2:
            return []
        data = read_json(path, {})
        return [data] + data.get('messages', [])[:30] if isinstance(data, dict) else []
    records = []
    with path.open('rb') as f:
        data = f.read(1024 * 1024)
    for line in data.splitlines():
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                records.append(row)
        except (ValueError, UnicodeDecodeError):
            pass
    return records


class Manager:
    def __init__(self, home, data):
        self.home, self.data = Path(home).resolve(), Path(data).resolve()
        self.data.mkdir(parents=True, exist_ok=True)
        self.data.chmod(0o700)
        self.settings = read_json(self.data / 'settings.json', {})
        self.annotations = read_json(self.data / 'annotations.json', {})
        self.contexts = {}
        self.roots = []
        self.warnings = []

    def default_roots(self):
        h = self.home
        return [
            ('codex', Path(os.environ.get('CODEX_HOME', h / '.codex'))),
            ('claude', Path(os.environ.get('CLAUDE_CONFIG_DIR', h / '.claude')) / 'projects'),
            ('gemini', h / '.gemini' / 'tmp'),
            ('antigravity', h / '.gemini' / 'antigravity-cli'),
            ('antigravity', h / '.gemini' / 'antigravity'),
        ]

    def candidates(self, provider, root):
        if provider == 'codex':
            for folder in ('sessions', 'archived_sessions'):
                yield from (root / folder).rglob('*.jsonl')
        elif provider == 'claude':
            yield from root.glob('*/*.jsonl')
        elif provider == 'gemini':
            yield from root.glob('*/chats/session-*.json')
        else:
            yield from (root / 'brain').glob('*/.system_generated/logs/transcript.jsonl')
            # IDE stores may only expose opaque protobufs; retain those as inspectable files.
            for f in (root / 'conversations').glob('*'):
                if f.suffix in ('.db', '.pb') and not (root / 'brain' / f.stem / '.system_generated/logs/transcript.jsonl').exists():
                    yield f

    def describe(self, provider, root, path):
        relative = path.relative_to(root).as_posix()
        key = hashlib.sha256(f'{provider}:{path}'.encode()).hexdigest()[:24]
        info = path.stat()
        sid = path.parent.parent.parent.name if path.name == 'transcript.jsonl' else path.stem
        ctx = {'id': key, 'sessionId': sid, 'provider': provider, 'title': sid,
               'project': '', 'model': '', 'path': str(path), 'root': str(root), 'relative': relative,
               'updated': datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(),
               'size': info.st_size, 'origin': 'local', 'readable': path.suffix in ('.json', '.jsonl')}
        records = sample_records(path) if ctx['readable'] else []
        for row in records:
            p = row.get('payload', {}) if row.get('type') in ('session_meta', 'turn_context') else row
            if isinstance(p, dict):
                ctx['project'] = p.get('cwd', p.get('project', ctx['project'])) or ctx['project']
                ctx['model'] = p.get('model', ctx['model']) or ctx['model']
                ctx['sessionId'] = p.get('sessionId', p.get('session_id', ctx['sessionId']))
            if row.get('type') == 'session_meta':
                ctx['sessionId'] = p.get('id', ctx['sessionId'])
            if row.get('summary') and isinstance(row['summary'], str):
                ctx['title'] = row['summary'][:160]
            msg = message(row, provider)
            if msg and msg['role'] == 'user' and ctx['title'] == sid:
                text = msg['content'].strip()
                if not text.startswith(('<environment_context>', '# AGENTS.md', '<local-command', '<command-name', '<system-reminder>')):
                    ctx['title'] = ' '.join(text.split())[:160]
        if provider == 'antigravity':
            meta = read_json(root / 'cache/conversation_metadata.json', {})
            # Metadata shape is not a public API; tolerate versions with nested maps.
            def locate(d, depth=0):
                if not isinstance(d, dict) or depth > 3:
                    return {}
                if isinstance(d.get(sid), dict):
                    return d[sid]
                for v in d.values():
                    found = locate(v, depth + 1)
                    if found:
                        return found
                return {}
            m = locate(meta)
            summary = m.get('summary', {})
            ctx['title'] = summary.get('Title', m.get('title', ctx['title']))
            uris = summary.get('WorkspaceURIs', [])
            if uris and isinstance(uris, list) and isinstance(uris[0], str):
                ctx['project'] = unquote(urlparse(uris[0]).path) if uris[0].startswith('file:') else uris[0]
            if not ctx['project']:
                dbpath = root / 'conversation_summaries.db'
                if dbpath.is_file():
                    try:
                        with sqlite3.connect(dbpath.as_uri() + '?mode=ro&immutable=1', uri=True) as db:
                            row = db.execute('SELECT title, workspace_uris FROM conversation_summaries WHERE conversation_id=?', (sid,)).fetchone()
                        if row:
                            ctx['title'] = row[0] or ctx['title']
                            uris = json.loads(row[1])
                            if uris and isinstance(uris[0], str):
                                ctx['project'] = unquote(urlparse(uris[0]).path) if uris[0].startswith('file:') else uris[0]
                    except (sqlite3.Error, ValueError, TypeError, KeyError):
                        pass
            if not ctx['project']:
                history = root / 'history.jsonl'
                if history.is_file():
                    for row in sample_records(history):
                        if row.get('conversationId') == sid and isinstance(row.get('workspace'), str):
                            ctx['project'] = row['workspace']
        return ctx

    def scan(self):
        self.contexts, self.roots, self.warnings = {}, [], []
        configured = self.default_roots() + [(x['provider'], Path(x['path']).expanduser()) for x in self.settings.get('roots', [])]
        seen = set()
        for provider, root in configured:
            root = root.resolve()
            if (provider, str(root)) in seen:
                continue
            seen.add((provider, str(root)))
            location = {'provider': provider, 'path': str(root), 'exists': root.is_dir(), 'count': 0}
            self.roots.append(location)
            if not root.is_dir():
                continue
            try:
                for path in self.candidates(provider, root):
                    try:
                        if path.is_symlink() or not path.resolve().is_relative_to(root):
                            continue
                        ctx = self.describe(provider, root, path)
                        self.contexts[ctx['id']] = ctx
                        location['count'] += 1
                    except (OSError, ValueError, TypeError, AttributeError) as e:
                        self.warnings.append(f'{path.name}: {e}')
            except OSError as e:
                self.warnings.append(str(e))
        for folder in (self.data / 'imports').glob('*'):
            manifest = read_json(folder / 'manifest.json', {})
            for item in manifest.get('contexts', []):
                ctx = dict(item['context'])
                ctx.update(id=folder.name + ':' + ctx['id'], path=str(folder / item['main']),
                           origin='imported', bundle=folder.name, importItem=item,
                           originalPath=ctx['path'])
                self.contexts[ctx['id']] = ctx
        return self.library()

    def library(self):
        items = []
        for ctx in self.contexts.values():
            public = {k: v for k, v in ctx.items() if k != 'importItem'}
            public.update(self.annotations.get(ctx['id'], {}))
            items.append(public)
        return {'contexts': sorted(items, key=lambda x: x['updated'], reverse=True),
                'locations': self.roots, 'warnings': self.warnings, 'dataDir': str(self.data)}

    def get(self, id):
        if id not in self.contexts:
            raise ValueError('Context no longer available. Refresh the library.')
        return self.contexts[id]

    def detail(self, id, cursor=0):
        ctx = self.get(id)
        path = Path(ctx['path'])
        messages, skipped = [], 0
        cursor = int(cursor)
        if cursor < 0:
            raise ValueError('Invalid cursor')
        if not ctx['readable']:
            return {'messages': [], 'next': None, 'notice': 'Binary provider state preserved. No readable transcript was found.'}
        if path.suffix == '.json':
            if path.stat().st_size > 128 * 1024**2:
                raise ValueError('This JSON context exceeds the 128 MiB viewer limit. Its original files can still be exported.')
            data = read_json(path)
            if not isinstance(data, dict):
                raise ValueError('Invalid session JSON')
            rows = data.get('messages', [])
            for row in rows[cursor:cursor + 200]:
                msg = message(row, ctx['provider'])
                if msg:
                    messages.append(msg)
            nxt = cursor + 200 if cursor + 200 < len(rows) else None
        else:
            with path.open('rb') as f:
                f.seek(cursor)
                budget = 0
                while len(messages) < 200 and budget < 8 * 1024**2:
                    line = f.readline(16 * 1024**2)
                    if not line:
                        break
                    budget += len(line)
                    if not line.endswith(b'\n') and len(line) == 16 * 1024**2:
                        while line and not line.endswith(b'\n'):
                            line = f.readline(1024 * 1024)
                        skipped += 1
                        continue
                    try:
                        msg = message(json.loads(line), ctx['provider'])
                        if msg:
                            messages.append(msg)
                    except (ValueError, UnicodeDecodeError, TypeError, AttributeError):
                        skipped += 1
                nxt = f.tell() if f.tell() < path.stat().st_size else None
        return {'messages': messages, 'next': nxt, 'skipped': skipped}

    def annotate(self, id, title=None, notes=None, tags=None, starred=None, archived=None):
        self.get(id)
        values = self.annotations.setdefault(id, {})
        for key, value in dict(title=title, notes=notes, tags=tags, starred=starred, archived=archived).items():
            if value is not None:
                if key in ('starred', 'archived'):
                    value = bool(value)
                elif key == 'tags':
                    if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
                        raise ValueError('Tags must be text')
                    value = [x.strip()[:60] for x in value[:30] if x.strip()]
                else:
                    value = str(value)[:20000 if key == 'notes' else 160]
                values[key] = value
        atomic_json(self.data / 'annotations.json', self.annotations)
        return self.library()

    def add_root(self, provider, path):
        if provider not in PROVIDERS or not Path(path).is_dir():
            raise ValueError('Choose an existing provider data directory')
        self.settings.setdefault('roots', []).append({'provider': provider, 'path': str(Path(path).resolve())})
        atomic_json(self.data / 'settings.json', self.settings)
        return self.scan()

    def native_files(self, ctx):
        if ctx['origin'] == 'imported':
            folder = self.data / 'imports' / ctx['bundle']
            return [(folder / f['archive'], f['relative']) for f in ctx['importItem']['files']]
        path, root = Path(ctx['path']), Path(ctx['root'])
        files = [path]
        if ctx['provider'] == 'claude':
            related = path.with_suffix('')
            if related.is_dir():
                files.extend(p for p in related.rglob('*') if p.is_file())
        elif ctx['provider'] == 'antigravity':
            sid = ctx['sessionId']
            brain = root / 'brain' / sid
            if brain.is_dir():
                files.extend(p for p in brain.rglob('*') if p.is_file())
            for suffix in ('.db', '.pb'):
                p = root / 'conversations' / (sid + suffix)
                if p.is_file():
                    files.append(p)
        result = []
        for p in dict.fromkeys(files):
            if p.is_symlink() or not p.resolve().is_relative_to(root):
                continue
            result.append((p, p.relative_to(root).as_posix()))
        return result

    def files(self, id):
        return [{'path': str(p), 'relative': rel, 'size': p.stat().st_size} for p, rel in self.native_files(self.get(id))]

    def export(self, ids, destination):
        if not ids or not isinstance(ids, list):
            raise ValueError('Select at least one context')
        contexts = [self.get(id) for id in dict.fromkeys(ids)]
        target = Path(destination)
        if target.exists():
            raise ValueError('Choose a new archive filename; existing backups are never overwritten.')
        manifest = {'format': 'gpt-manager', 'version': VERSION, 'created': now(), 'contexts': [],
                    'note': 'Contains private conversation data. Credentials and project files are not included. Provider indexes may need rebuilding after native restore.'}
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.data) as temp:
            temp = Path(temp)
            output = temp / 'bundle.zip'
            with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=3, allowZip64=True) as z:
                for i, ctx in enumerate(contexts):
                    public = {k: v for k, v in ctx.items() if k not in ('importItem', 'bundle', 'originalPath')}
                    public.update(self.annotations.get(ctx['id'], {}))
                    item = {'context': public, 'files': [], 'main': ''}
                    for j, (source, rel) in enumerate(self.native_files(ctx)):
                        arc = f'contexts/{i}/files/{rel}'
                        snapshot = temp / f'snapshot-{i}-{j}'
                        # SQLite's backup API includes committed WAL data in a portable standalone DB.
                        if source.suffix == '.db':
                            with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as src:
                                with sqlite3.connect(snapshot) as dst:
                                    src.backup(dst)
                        else:
                            # Bound reads to initial size so an actively appended transcript cannot run forever.
                            remaining = source.stat().st_size
                            with source.open('rb') as inp, snapshot.open('wb') as out:
                                while remaining:
                                    chunk = inp.read(min(remaining, 1024 * 1024))
                                    if not chunk:
                                        raise ValueError(f'{source.name} changed during export. Retry when the provider is idle.')
                                    out.write(chunk)
                                    remaining -= len(chunk)
                        entry = {'archive': arc, 'relative': rel, 'size': snapshot.stat().st_size, 'sha256': digest(snapshot)}
                        z.write(snapshot, arc)
                        snapshot.unlink()
                        item['files'].append(entry)
                        if source == Path(ctx['path']):
                            item['main'] = arc
                    if not item['main']:
                        raise ValueError('The main transcript is unavailable')
                    manifest['contexts'].append(item)
                z.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
            # Exclusive create, including when another process races the dialog.
            created = False
            try:
                with target.open('xb') as out, output.open('rb') as inp:
                    created = True
                    os.chmod(target, 0o600)
                    shutil.copyfileobj(inp, out)
            except FileExistsError:
                raise ValueError('Archive already exists')
            except Exception:
                if created:
                    target.unlink(missing_ok=True)
                raise
        return {'path': str(target), 'count': len(contexts), 'size': target.stat().st_size}

    @staticmethod
    def safe_relative(name):
        p = PurePosixPath(name)
        if not isinstance(name, str) or not name or '\\' in name or ':' in name or '\x00' in name or p.is_absolute() or any(x in ('..', '.') for x in name.split('/')):
            raise ValueError('Unsafe archive path')
        return p

    @staticmethod
    def validate_layout(ctx, files, main):
        primary = next((f['relative'] for f in files if f['archive'] == main), None)
        if primary is None:
            raise ValueError('Missing main transcript')
        p = PurePosixPath(primary)
        provider = ctx['provider']
        for f in files:
            r = PurePosixPath(f['relative'])
            if provider == 'codex':
                valid = r == p and len(r.parts) >= 2 and r.parts[0] in ('sessions', 'archived_sessions') and r.suffix == '.jsonl'
            elif provider == 'claude':
                valid = len(p.parts) == 2 and p.suffix == '.jsonl' and (r == p or r.is_relative_to(p.with_suffix('')))
            elif provider == 'gemini':
                valid = r == p and len(p.parts) == 3 and p.parts[1] == 'chats' and p.suffix == '.json'
            else:
                sid = ctx['sessionId']
                if not sid or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in sid):
                    raise ValueError('Invalid Antigravity session ID')
                valid = (len(r.parts) > 2 and r.parts[:2] == ('brain', sid)) or (len(r.parts) == 2 and r.parts[0] == 'conversations' and r.stem == sid and r.suffix in ('.db', '.pb'))
            if not valid:
                raise ValueError('File is outside the provider context layout')

    def validate(self, archive):
        infos = archive.infolist()
        if len(infos) > MAX_FILES or sum(i.file_size for i in infos) > MAX_TOTAL:
            raise ValueError('Archive exceeds limits (50,000 files / 16 GiB)')
        names = set()
        for info in infos:
            self.safe_relative(info.filename)
            if info.filename in names or stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError('Duplicate entries and symlinks are not allowed')
            names.add(info.filename)
        if 'manifest.json' not in names or archive.getinfo('manifest.json').file_size > 8 * 1024**2:
            raise ValueError('Missing or oversized manifest')
        m = json.loads(archive.read('manifest.json'))
        if m.get('format') != 'gpt-manager' or m.get('version') != VERSION:
            raise ValueError('Unsupported context archive')
        if not isinstance(m.get('contexts'), list) or not m['contexts']:
            raise ValueError('Archive contains no contexts')
        expected = {'manifest.json'}
        ids = set()
        for item in m['contexts']:
            ctx = item['context']
            if ctx.get('provider') not in PROVIDERS or not isinstance(ctx.get('id'), str) or ctx['id'] in ids:
                raise ValueError('Invalid or duplicate context identity')
            ids.add(ctx['id'])
            if any(not isinstance(ctx.get(k), str) for k in ('title', 'sessionId', 'project', 'model', 'path', 'root', 'relative', 'updated')):
                raise ValueError('Invalid context metadata')
            if not isinstance(ctx.get('size'), int) or not isinstance(ctx.get('readable'), bool):
                raise ValueError('Invalid context metadata')
            for key in ('notes',):
                if key in ctx and not isinstance(ctx[key], str):
                    raise ValueError('Invalid annotation')
            if 'tags' in ctx and (not isinstance(ctx['tags'], list) or any(not isinstance(t, str) for t in ctx['tags'])):
                raise ValueError('Invalid tags')
            if any(key in ctx and not isinstance(ctx[key], bool) for key in ('starred', 'archived')):
                raise ValueError('Invalid annotation')
            for f in item['files']:
                self.safe_relative(f['relative'])
            self.validate_layout(ctx, item['files'], item['main'])
            rels, paths = set(), set()
            for f in item['files']:
                self.safe_relative(f['relative'])
                self.safe_relative(f['archive'])
                if f['archive'] in expected or f['relative'] in rels or not f['archive'].startswith('contexts/'):
                    raise ValueError('Duplicate or invalid file mapping')
                if f['archive'] not in names or archive.getinfo(f['archive']).file_size != f['size']:
                    raise ValueError('Missing file or size mismatch')
                h = hashlib.sha256()
                with archive.open(f['archive']) as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                        h.update(chunk)
                if h.hexdigest() != f['sha256']:
                    raise ValueError('Checksum mismatch: ' + f['relative'])
                expected.add(f['archive']); rels.add(f['relative']); paths.add(f['archive'])
            if item['main'] not in paths:
                raise ValueError('Missing main transcript')
        if names != expected:
            raise ValueError('Archive includes undeclared files')
        return m

    def preview(self, path):
        with zipfile.ZipFile(path) as z:
            m = self.validate(z)
        return {'created': m['created'], 'contexts': [x['context'] for x in m['contexts']],
                'files': sum(len(x['files']) for x in m['contexts']),
                'size': sum(f['size'] for x in m['contexts'] for f in x['files'])}

    def import_archive(self, path):
        imports = self.data / 'imports'
        imports.mkdir(exist_ok=True)
        key = uuid.uuid4().hex
        with tempfile.TemporaryDirectory(dir=self.data) as temp:
            stage = Path(temp) / 'bundle'
            stage.mkdir()
            with zipfile.ZipFile(path) as z:
                m = self.validate(z)
                for item in m['contexts']:
                    for f in item['files']:
                        target = stage / f['archive']
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with z.open(f['archive']) as src, target.open('xb') as out:
                            shutil.copyfileobj(src, out)
                atomic_json(stage / 'manifest.json', m)
            stage.rename(imports / key)
        return self.scan()

    def restore(self, id, destination):
        ctx = self.get(id)
        if ctx['origin'] != 'imported':
            raise ValueError('Native restore is available for imported contexts')
        root = Path(destination).resolve()
        if not root.is_dir():
            raise ValueError('Choose an existing destination directory')
        files = self.native_files(ctx)
        targets = []
        for source, relative in files:
            self.safe_relative(relative)
            target = root / relative
            if not target.resolve().is_relative_to(root):
                raise ValueError('Destination escapes the chosen root through a symlink')
            for parent in [target, *target.parents]:
                if parent == root:
                    break
                if parent.is_symlink():
                    raise ValueError('Destination contains a symlink')
            if target.exists() or target.with_name(target.name + '-wal').exists() or target.with_name(target.name + '-shm').exists():
                raise ValueError('Restore conflict: ' + str(target) + '. No files were written. Choose an empty staging directory.')
            targets.append((source, target))
        written = []
        try:
            for source, target in targets:
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open('xb') as out:
                    written.append(target)
                    os.chmod(target, 0o600)
                    with source.open('rb') as inp:
                        shutil.copyfileobj(inp, out)
        except Exception:
            for path in written:
                path.unlink(missing_ok=True)
            raise
        return {'count': len(written), 'path': str(root)}
