"""Run on each target OS with Python 3.12+ and PyInstaller installed."""
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
args = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir', '--name', 'gpt-manager-backend', '--distpath', str(root / 'build/backend'), '--workpath', str(root / 'build/pyinstaller'), '--specpath', str(root / 'build'), '--paths', str(root / 'backend'), '--hidden-import', 'rpc', '--hidden-import', 'session_lock']
if sys.platform != 'win32': args.extend(['--hidden-import', 'terminal'])
# SSH transmits these source files to POSIX hosts; they must also exist in frozen builds.
for name in ('manager', 'session_lock', 'workspace_transfer', 'activity', 'ssh_agent'):
    args.extend(['--add-data', str(root / 'backend' / (name + '.py')) + ':.'])
subprocess.run([*args, str(root / 'backend/entry.py')], cwd=root, check=True)
