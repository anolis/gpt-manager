import base64
import json
import os
import select
import subprocess
import sys
import unittest
from pathlib import Path


@unittest.skipIf(os.name != 'posix', 'POSIX PTY')
class TerminalTests(unittest.TestCase):
    def test_real_pty_input_resize_and_exit(self):
        bridge = Path(__file__).resolve().parents[1] / 'backend/terminal.py'
        code = 'import os,sys; print("READY",os.isatty(0),flush=True); line=input(); print("REPLY:"+line,flush=True)'
        proc = subprocess.Popen([sys.executable, str(bridge), '/tmp', sys.executable, '-c', code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            output = ''
            for _ in range(30):
                ready, _, _ = select.select([proc.stdout], [], [], 1)
                if not ready:
                    if proc.poll() is not None: break
                    continue
                line = proc.stdout.readline()
                if not line: break
                msg = json.loads(line)
                if 'data' in msg: output += base64.b64decode(msg['data']).decode()
                if 'READY True' in output: break
            self.assertIn('READY True', output)
            commands = json.dumps({'type': 'resize', 'cols': 120, 'rows': 30}) + '\n' + json.dumps({'type': 'input', 'data': 'hello terminal\n'}) + '\n'
            proc.stdin.write(commands.encode()); proc.stdin.flush()
            for _ in range(30):
                ready, _, _ = select.select([proc.stdout], [], [], 1)
                if not ready:
                    if proc.poll() is not None: break
                    continue
                line = proc.stdout.readline()
                if not line: break
                msg = json.loads(line)
                if 'data' in msg: output += base64.b64decode(msg['data']).decode()
                if 'exit' in msg: break
            proc.wait(timeout=5)
            self.assertIn('REPLY:hello terminal', output)
            self.assertEqual(proc.returncode, 0)
        finally:
            if proc.poll() is None: proc.kill(); proc.wait()
            proc.stdin.close(); proc.stdout.close(); proc.stderr.close()
