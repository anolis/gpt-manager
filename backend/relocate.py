"""Context file relocation with a durable archive and conflict-safe rollback."""
import hashlib
import os
import shutil
import uuid
from pathlib import Path

try:
    from .manager import atomic_json, digest
except ImportError:
    from manager import atomic_json, digest


def project_folder(manager, id, destination):
    context = manager.get(id)
    target = Path(destination).expanduser().resolve()
    if not target.is_dir():
        raise ValueError('Choose an existing project folder')
    values = manager.annotations.setdefault(id, {})
    values['project'] = str(target)
    atomic_json(manager.data / 'annotations.json', manager.annotations)
    return manager.library()


def plan_move(manager, id, destination):
    context = manager.get(id)
    if context['origin'] != 'local':
        raise ValueError('Imported contexts are managed library copies. Use Restore files to place them elsewhere.')
    root = Path(destination).expanduser().resolve()
    source_root = Path(context['root']).resolve()
    if not root.is_dir() or root == source_root:
        raise ValueError('Choose a different existing context-store directory')
    if root == manager.data or root.is_relative_to(manager.data):
        raise ValueError('Choose a destination outside GPT Manager’s private storage')
    files = manager.native_files(context)
    if not files:
        raise ValueError('No original context files are available')
    planned = []
    for source, relative in files:
        manager.safe_relative(relative)
        target = root / relative
        if source.is_symlink() or not source.resolve().is_relative_to(source_root):
            raise ValueError('Source context contains a symlink')
        if target == source or target.exists() or target.is_symlink():
            raise ValueError('Move conflict: ' + str(target))
        if not target.resolve().is_relative_to(root):
            raise ValueError('Destination escapes the selected folder')
        for parent in target.parents:
            if parent == root:
                break
            if parent.is_symlink():
                raise ValueError('Destination contains a symlink')
        if source.suffix == '.db' and source.with_name(source.name + '-wal').exists():
            raise ValueError('This conversation database has a WAL file. Close the provider and checkpoint it before moving; use Export for a live snapshot.')
        planned.append({'source': str(source), 'destination': str(target), 'relative': relative, 'size': source.stat().st_size})
    return {'id': id, 'provider': context['provider'], 'sourceRoot': str(source_root), 'destinationRoot': str(root), 'files': planned, 'size': sum(f['size'] for f in planned)}


def move_files(manager, id, destination):
    plan = plan_move(manager, id, destination)
    context = manager.get(id)
    root = Path(plan['destinationRoot'])
    main = root / context['relative']
    new_id = hashlib.sha256(f"{context['provider']}:{main}".encode()).hexdigest()[:24]
    backups = manager.data / 'move-backups'
    backups.mkdir(exist_ok=True)
    backup = backups / (uuid.uuid4().hex + '.gptctx')
    manager.export([id], backup)
    written, deleted = [], []
    fingerprints = {}
    try:
        for file in plan['files']:
            source, target = Path(file['source']), Path(file['destination'])
            info = source.stat()
            fingerprints[str(source)] = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, digest(source))
            target.parent.mkdir(parents=True, exist_ok=True)
            # Exclusive creation prevents replacing a file introduced after the preview.
            with target.open('xb') as out:
                target.chmod(0o600)
                written.append(target)
                with source.open('rb') as inp:
                    shutil.copyfileobj(inp, out)
            os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))
            if digest(target) != fingerprints[str(source)][-1]:
                raise ValueError('A context file changed during the move. Close the provider and retry.')
        # Confirm the entire source set before removing anything.
        for file in plan['files']:
            source = Path(file['source']); info = source.stat()
            current = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, digest(source))
            if current != fingerprints[str(source)]:
                raise ValueError('A context file changed during the move. Close the provider and retry.')
        for file in plan['files']:
            source = Path(file['source'])
            source.unlink()
            deleted.append(file)
    except Exception:
        rollback_errors = []
        for file in deleted:
            try:
                with Path(file['source']).open('xb') as out, Path(file['destination']).open('rb') as inp:
                    shutil.copyfileobj(inp, out)
            except OSError:
                rollback_errors.append(file['source'])
        if not rollback_errors:
            for target in written:
                target.unlink(missing_ok=True)
        if rollback_errors:
            raise ValueError(f'Move interrupted; verified destination copies and recovery archive are retained at {backup}. Some source files could not be restored.') from None
        raise
    # Persist the new discovery root and carry organization to the new stable path identity.
    manager.settings.setdefault('roots', [])
    new_root = {'provider': context['provider'], 'path': str(root)}
    if new_root not in manager.settings['roots']:
        manager.settings['roots'].append(new_root)
    manager.annotations[new_id] = dict(manager.annotations.get(id, {}))
    manager.annotations[new_id]['moveBackup'] = str(backup)
    manager.annotations.pop(id, None)
    atomic_json(manager.data / 'settings.json', manager.settings)
    atomic_json(manager.data / 'annotations.json', manager.annotations)
    library = manager.scan()
    return {'library': library, 'id': new_id, 'previousId': id, 'backup': str(backup), 'count': len(written), 'path': str(root)}
