"""Bounded workspace snapshots, preserving internal relative symlinks."""
import os
import stat
import zipfile
from pathlib import Path, PurePosixPath

WORKSPACE_LIMIT = 16 * 1024**3
WORKSPACE_FILES = 100000


def workspace_entries(root):
    entries = []
    for parent, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(parent) / name
            entries.append(path)
            if len(entries) > WORKSPACE_FILES:
                raise ValueError('Workspace exceeds 100,000 entries; use an existing local checkout.')
    return sorted(entries)


def snapshot_workspace(root, archive_path):
    root = Path(root).resolve()
    if not root.is_dir() or root == Path.home() or root == Path('/'):
        raise ValueError('Choose a dedicated project directory before transferring a work folder.')
    if (root / '.git').is_file():
        raise ValueError('This project uses an external Git directory or linked worktree. Choose an existing local checkout instead of copying the work folder.')
    entries = workspace_entries(root)
    fingerprints = {}
    total = 0
    with zipfile.ZipFile(archive_path, 'w', compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        for path in entries:
            info = path.lstat()
            fingerprints[path] = (info.st_ino, info.st_size, info.st_mtime_ns)
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                link = os.readlink(path)
                if Path(link).is_absolute() or not (path.parent / link).resolve().is_relative_to(root):
                    raise ValueError(f'Workspace contains a symlink outside the project: {relative}. Use an existing local folder instead.')
                entry = zipfile.ZipInfo(relative)
                entry.create_system = 3
                entry.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(entry, link)
            elif path.is_dir():
                archive.write(path, relative + '/')
            elif stat.S_ISREG(info.st_mode):
                total += info.st_size
                if total > WORKSPACE_LIMIT:
                    raise ValueError('Workspace exceeds 16 GiB; use an existing local checkout.')
                archive.write(path, relative)
            else:
                raise ValueError(f'Workspace contains a special file: {relative}. Use an existing local checkout.')
    if workspace_entries(root) != entries or any((p.lstat().st_ino, p.lstat().st_size, p.lstat().st_mtime_ns) != value for p, value in fingerprints.items()):
        raise ValueError('Work folder changed during capture. Stop edits/builds and retry.')


def extract_workspace(archive_path, destination):
    destination = Path(destination).resolve()
    if any(destination.iterdir()):
        raise ValueError('Workspace destination must be empty.')
    with zipfile.ZipFile(archive_path) as archive:
        entries = archive.infolist()
        if len(entries) > WORKSPACE_FILES or sum(i.file_size for i in entries) > WORKSPACE_LIMIT:
            raise ValueError('Workspace archive exceeds the transfer limits.')
        seen, links = set(), []
        for entry in entries:
            path = PurePosixPath(entry.filename)
            if path.is_absolute() or '..' in path.parts or '\\' in entry.filename or not path.parts or str(path) in seen:
                raise ValueError('Unsafe or duplicate workspace path')
            seen.add(str(path))
            target = destination.joinpath(*path.parts)
            mode = entry.external_attr >> 16
            if stat.S_ISLNK(mode):
                if entry.file_size > 4096:
                    raise ValueError('Invalid workspace symlink')
                link = archive.read(entry).decode()
                if Path(link).is_absolute() or not (target.parent / link).resolve().is_relative_to(destination):
                    raise ValueError('Workspace symlink points outside the project')
                links.append((target, link))
            elif entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif stat.S_IFMT(mode) not in (0, stat.S_IFREG):
                raise ValueError('Unsupported workspace file type')
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as src, target.open('xb') as out:
                    while chunk := src.read(1024 * 1024):
                        out.write(chunk)
                target.chmod(0o700 if mode & 0o111 else 0o600)
        for target, link in links:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(link)
