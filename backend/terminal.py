"""POSIX PTY bridge: JSON control on stdin, base64 terminal bytes on stdout."""
import base64
import errno
import fcntl
import json
import os
import pty
import selectors
import signal
import struct
import sys
import termios


def emit(value):
    print(json.dumps(value), flush=True)


def main():
    cwd, command, *args = sys.argv[1:]
    pid, master = pty.fork()
    if pid == 0:
        os.chdir(cwd)
        os.environ['TERM'] = 'xterm-256color'
        os.environ.pop('ELECTRON_RUN_AS_NODE', None)
        os.execvpe(command, [command, *args], os.environ)
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 100, 0, 0))
    selector = selectors.DefaultSelector()
    selector.register(master, selectors.EVENT_READ)
    selector.register(sys.stdin.fileno(), selectors.EVENT_READ)
    control = b''
    def terminate(*_):
        try:
            os.killpg(pid, signal.SIGHUP)
        except ProcessLookupError:
            pass
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    try:
        while True:
            for key, _ in selector.select():
                if key.fd == master:
                    try:
                        data = os.read(master, 65536)
                    except OSError as e:
                        if e.errno == errno.EIO:
                            data = b''
                        else:
                            raise
                    if not data:
                        _, status = os.waitpid(pid, 0)
                        emit({'exit': os.waitstatus_to_exitcode(status)})
                        return
                    emit({'data': base64.b64encode(data).decode('ascii')})
                else:
                    chunk = os.read(sys.stdin.fileno(), 65536)
                    if not chunk:
                        terminate()
                    control += chunk
                    while b'\n' in control:
                        line, control = control.split(b'\n', 1)
                        msg = json.loads(line)
                        if msg.get('type') == 'input':
                            data = msg['data'].encode('utf-8')
                            while data:
                                data = data[os.write(master, data):]
                        elif msg.get('type') == 'resize':
                            rows, cols = max(2, min(300, int(msg['rows']))), max(2, min(500, int(msg['cols'])))
                            fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))
    finally:
        selector.close()
        os.close(master)
        try:
            os.killpg(pid, signal.SIGHUP)
        except ProcessLookupError:
            pass


if __name__ == '__main__':
    main()
