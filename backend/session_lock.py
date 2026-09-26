"""Cooperative per-host resume locks and explicit handoff reservations."""
import hashlib
import json
import os
import signal
import subprocess
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path


def lock_paths(provider, session_id, create=True):
    root = Path.home() / '.local/state/gpt-manager/session-locks'
    if create:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = hashlib.sha256(f'{provider}:{session_id}'.encode()).hexdigest()
    return root / name, root / (name + '.handoff')


def handoff_status(provider, session_id):
    _, marker = lock_paths(provider, session_id, create=False)
    if not marker.exists():
        return None
    try:
        value = json.loads(marker.read_text())
        return {'target': value['target'], 'state': value['state']}
    except (OSError, ValueError, KeyError):
        return {'target': 'another machine', 'state': 'reserved'}


@contextmanager
def context_lock(provider, session_id, token=None):
    path, marker = lock_paths(provider, session_id)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        try:
            if os.name == 'nt':
                import msvcrt
                if os.fstat(fd).st_size == 0: os.write(fd, b'0')
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, PermissionError):
            raise ValueError('This context is already running through GPT Manager on this machine. Close that session before resuming it here.') from None
        if marker.exists():
            state = json.loads(marker.read_text())
            if not token or token != state.get('token'):
                raise ValueError(f"This context was handed off or reserved for {state.get('target', 'another machine')}. Stop the destination session and explicitly release the handoff before resuming the source.")
        yield marker
    finally:
        os.close(fd)


def reserve_handoff(provider, session_id, token, target):
    if not isinstance(token, str) or len(token) != 32:
        raise ValueError('Invalid handoff token')
    with context_lock(provider, session_id) as marker:
        with marker.open('x') as out:
            os.chmod(marker, 0o600)
            json.dump({'token': token, 'target': str(target)[:200], 'state': 'reserved'}, out)
    return True


def update_handoff(provider, session_id, token, finish=False, force=False):
    path, marker = lock_paths(provider, session_id)
    if not marker.exists():
        return True
    # Explicit recovery can obtain the existing token, but still respects live locks.
    if force:
        token = json.loads(marker.read_text())['token']
    with context_lock(provider, session_id, token):
        if finish:
            state = json.loads(marker.read_text())
            state['state'] = 'moved'
            temporary = marker.with_suffix('.tmp-' + uuid.uuid4().hex)
            temporary.write_text(json.dumps(state))
            temporary.chmod(0o600)
            temporary.replace(marker)
        else:
            marker.unlink()
    return True


def resume_locked(provider, session_id, cwd, command, args, env=None):
    with context_lock(provider, session_id):
        child = subprocess.Popen([command, *args], cwd=cwd, env=env)
        def forward(signum, frame):
            if child.poll() is None:
                child.send_signal(signum)
        for signum in ([signal.SIGHUP] if hasattr(signal, 'SIGHUP') else []) + [signal.SIGTERM, signal.SIGINT]:
            signal.signal(signum, forward)
        return child.wait()


if __name__ == '__main__':
    try:
        sys.exit(resume_locked(*sys.argv[1:5], sys.argv[5:]))
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
