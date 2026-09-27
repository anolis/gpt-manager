"""Per-user Linux installation, atomic upgrades and desktop integration."""
import argparse
import fcntl
import hashlib
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path
from urllib.request import urlopen

APP_ID = 'io.github.anolis.gpt-manager'
MARKER = '.gpt-manager-install.json'
SOURCE_FILES = ('backend', 'desktop', 'ui', 'tools', 'package.json', 'package-lock.json', 'LICENSE', 'install-linux.sh')
NODE_VERSION = 'v24.18.0'
NODE_HASHES = {
    'x64': '783130984963db7ba9cbd01089eaf2c2efb055c7c1693c943174b967b3050cb8',
    'arm64': '6b4484c2190274175df9aa8f28e2d758a819cb1c1fe6ab481e2f95b463ab8508',
}


def run(args, **kwargs):
    return subprocess.run([str(x) for x in args], check=True, **kwargs)


def atomic_text(path, text, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    try:
        temporary.write_text(text, encoding='utf-8'); temporary.chmod(mode); temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def desktop_value(value):
    return str(value).replace('\\', '\\\\').replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t')


def desktop_exec(path):
    quoted = ''.join('\\' + c if c in '\\"`$' else c for c in str(path)).replace('%', '%%')
    return desktop_value('"' + quoted + '"')


class Installer:
    def __init__(self, home=None, data_home=None):
        self.home = Path(home or Path.home()).absolute()
        self.data_home = Path(data_home or os.environ.get('XDG_DATA_HOME', str(self.home / '.local/share'))).absolute()
        self.root = self.data_home / 'gpt-manager-app'
        if any(c in str(self.root) + str(self.home) for c in '\n\r\0'): raise ValueError('Installation paths cannot contain line breaks or null characters.')
        self.bin = self.home / '.local/bin'
        self.desktop = self.data_home / 'applications' / (APP_ID + '.desktop')
        self.icons = [self.data_home / 'icons/hicolor' / size / 'apps' / (APP_ID + extension)
                      for size, extension in [('scalable', '.svg'), ('1024x1024', '.png')]]

    def owned(self):
        if self.root.is_symlink(): raise ValueError('Installation directory must not be a symlink.')
        if not self.root.exists(): return False
        try: data = json.loads((self.root / MARKER).read_text())
        except (OSError, ValueError): raise ValueError(f'Refusing to overwrite an unrecognized directory: {self.root}') from None
        if data.get('appId') != APP_ID: raise ValueError('Installation marker is not GPT Manager’s.')
        return True

    def lock(self):
        self.root.parent.mkdir(parents=True, exist_ok=True)
        lock = (self.root.parent / '.gpt-manager-installer.lock').open('a')
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close(); raise ValueError('Another GPT Manager install or uninstall is running.') from None
        return lock

    def owns_launcher(self, path):
        marker = 'X-GPT-Manager-Install=' + desktop_value(self.root) if path == self.desktop else '# ' + str(self.root)
        return marker in path.read_text().splitlines()

    def install(self, source, allow_system_changes=False):
        source = Path(source).resolve()
        for name in SOURCE_FILES:
            if not (source / name).exists(): raise ValueError(f'Missing application source: {name}')
        with self.lock():
            self.owned()
            self.root.mkdir(parents=True, exist_ok=True)
            if not (self.root / MARKER).exists(): atomic_text(self.root / MARKER, json.dumps({'appId': APP_ID}))
            for target in (self.bin / 'gpt-manager', self.bin / 'gpt-manager-uninstall', self.desktop):
                if target.exists() and not self.owns_launcher(target):
                    raise ValueError(f'Refusing to replace an unrelated launcher: {target}')
            versions = self.root / 'versions'; versions.mkdir(exist_ok=True)
            stage = Path(tempfile.mkdtemp(prefix='install-', dir=versions))
            activated = False
            try:
                for name in SOURCE_FILES:
                    src, dst = source / name, stage / name
                    if src.is_dir(): shutil.copytree(src, dst, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
                    else: shutil.copy2(src, dst)
                print('[3/6] Installing a verified, dedicated Node runtime…', flush=True)
                self.node_runtime(stage)
                print('[4/6] Installing locked application dependencies (including Electron)…', flush=True)
                self.dependencies(stage)
                print('[5/6] Checking the runtime and Linux sandbox…', flush=True)
                self.verify(stage)
                self.sandbox(allow_system_changes)
                print('[6/6] Registering GPT Manager in the application menu…', flush=True)
                self.integrate(stage)
                pending = self.root / ('current-' + uuid.uuid4().hex)
                pending.symlink_to(stage.relative_to(self.root)); pending.replace(self.root / 'current')
                activated = True
                version = json.loads((stage / 'package.json').read_text())['version']
                self.refresh_desktop()
                print(f'\nGPT Manager {version} installed. Open “GPT Manager” from your application menu.')
                print(f'Command: {self.bin / "gpt-manager"}\nUninstall: {self.bin / "gpt-manager-uninstall"}')
                print('Run the installer again to update. Earlier app versions are retained for running sessions.')
            finally:
                if not activated: shutil.rmtree(stage)

    def node_runtime(self, stage):
        stage = stage.resolve()
        arch = {'x86_64': 'x64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(platform.machine())
        if not arch: raise ValueError('Supported CPUs: x86_64 and ARM64.')
        name = f'node-{NODE_VERSION}-linux-{arch}'
        archive = stage / 'node.tar.gz'; digest = hashlib.sha256(); received = 0; last = -1
        cache = self.root / 'cache' / (name + '.tar.gz')
        if cache.is_file() and cache.stat().st_size <= 150 * 1024**2:
            with cache.open('rb') as src:
                while chunk := src.read(256 * 1024): digest.update(chunk)
            if digest.hexdigest() == NODE_HASHES[arch]:
                print('  Reusing the verified Node download.', flush=True); shutil.copyfile(cache, archive)
        if not archive.exists():
            digest = hashlib.sha256()
            with urlopen(f'https://nodejs.org/dist/{NODE_VERSION}/{name}.tar.gz', timeout=30) as response, archive.open('wb') as out:
                total = int(response.headers.get('Content-Length', 0))
                while chunk := response.read(256 * 1024):
                    received += len(chunk)
                    if received > 150 * 1024**2: raise ValueError('Node download exceeded its size limit.')
                    digest.update(chunk); out.write(chunk)
                    progress = received * 100 // total if total else received // 1024**2
                    if progress // 10 != last:
                        print(f'  Node download: {progress}{"%" if total else " MB"}', flush=True); last = progress // 10
        if digest.hexdigest() != NODE_HASHES[arch]: raise ValueError('Node checksum mismatch. Installation stopped; rerun to retry.')
        cache.parent.mkdir(exist_ok=True)
        pending = cache.with_suffix('.download')
        try: shutil.copyfile(archive, pending); pending.replace(cache)
        finally: pending.unlink(missing_ok=True)
        with tarfile.open(archive) as bundle:
            # Node publishes links inside this one top-level directory. Reject escapes/devices.
            for member in bundle.getmembers():
                target = (stage / member.name).resolve()
                if not target.is_relative_to(stage / name) or member.isdev(): raise ValueError('Unsafe Node archive entry.')
                if member.issym() and not (target.parent / member.linkname).resolve().is_relative_to(stage / name): raise ValueError('Unsafe Node archive link.')
                if member.islnk() and not (stage / member.linkname).resolve().is_relative_to(stage / name): raise ValueError('Unsafe Node archive hard link.')
            bundle.extractall(stage, filter='data')
        (stage / name).rename(stage / 'node'); archive.unlink()

    def dependencies(self, stage):
        env = {k: v for k, v in os.environ.items() if not k.lower().startswith('npm_config_') and k not in ('ELECTRON_SKIP_BINARY_DOWNLOAD', 'ELECTRON_RUN_AS_NODE', 'ELECTRON_MIRROR', 'ELECTRON_CUSTOM_DIR', 'ELECTRON_CUSTOM_FILENAME')}
        empty = stage / 'empty.npmrc'; empty.write_text('')
        env.update(PATH=str(stage / 'node/bin') + os.pathsep + env.get('PATH', ''), NPM_CONFIG_USERCONFIG=str(empty), NPM_CONFIG_GLOBALCONFIG=str(stage / 'empty-global.npmrc'))
        (stage / 'empty-global.npmrc').write_text('')
        run([stage / 'node/bin/node', stage / 'node/lib/node_modules/npm/bin/npm-cli.js', 'ci', '--registry=https://registry.npmjs.org', '--no-audit', '--no-fund', '--ignore-scripts'], cwd=stage, env=env)
        # Linux terminals use Python PTYs; node-pty's native build is Windows-only.
        # Explicitly install Electron even when npm's lifecycle-script policy blocks it.
        run([stage / 'node/bin/node', stage / 'node_modules/electron/install.js'], cwd=stage, env=env)

    def verify(self, stage):
        run([stage / 'node/bin/node', '--version'])
        run([sys.executable, '-c', 'import sqlite3, ssl, pty, backend.manager'], cwd=stage)
        electron = stage / 'node_modules/electron/dist/electron'
        if not electron.is_file(): raise ValueError('Electron download is missing. Check your network and retry.')
        result = run(['ldd', electron], capture_output=True, text=True)
        missing = [line.strip() for line in result.stdout.splitlines() if 'not found' in line]
        if missing: raise ValueError('Missing desktop libraries: ' + '; '.join(missing) + '. Re-run without --skip-system-deps.')
        env = dict(os.environ, ELECTRON_RUN_AS_NODE='1')
        run([electron, '--version'], env=env)

    def sandbox(self, allow_system_changes):
        for setting in ('/proc/sys/kernel/unprivileged_userns_clone', '/proc/sys/user/max_user_namespaces'):
            path = Path(setting)
            if path.exists() and path.read_text().strip() == '0':
                raise ValueError('This host disables user namespaces required by Electron’s sandbox. Ask an administrator to enable them before installing.')
        restriction = Path('/proc/sys/kernel/apparmor_restrict_unprivileged_userns')
        if not restriction.exists() or restriction.read_text().strip() != '1': return
        target = Path('/etc/apparmor.d') / f'gpt-manager-{os.getuid()}'
        attachment = str(self.root / 'versions').replace('\\', '\\\\').replace('"', '\\"')
        if any(c in attachment for c in '\n\r*?[]{}'):
            raise ValueError('This installation path cannot be used in an AppArmor profile.')
        content = f'# {APP_ID}\nabi <abi/4.0>,\ninclude <tunables/global>\nprofile gpt-manager-{os.getuid()} "{attachment}/*/node_modules/electron/dist/electron" flags=(unconfined) {{\n  userns,\n}}\n'
        if target.exists() and target.read_text() == content: return
        if not allow_system_changes:
            raise ValueError('This host requires a GPT Manager AppArmor profile. Re-run without --skip-system-deps; Electron’s sandbox will stay enabled.')
        if target.exists() and not target.read_text().startswith('# ' + APP_ID + '\n'):
            raise ValueError(f'Refusing to replace an unrelated AppArmor profile: {target}')
        print('  Allowing user namespaces only for GPT Manager’s Electron binary (sudo).', flush=True)
        with tempfile.TemporaryDirectory(prefix='gpt-manager-apparmor-') as folder:
            profile = Path(folder) / target.name; profile.write_text(content)
            run(['sudo', 'install', '-m', '644', profile, target])
            run(['sudo', 'apparmor_parser', '-r', target])

    def integrate(self, stage):
        current = self.root / 'current'
        launcher = f'''#!/usr/bin/env bash
# {self.root}
set -euo pipefail
app_dir=$(readlink -f -- {shlex.quote(str(current))})
export PATH="$HOME/.local/bin:$app_dir/node/bin:$PATH"
export GPT_MANAGER_PYTHON={shlex.quote(sys.executable)}
unset ELECTRON_RUN_AS_NODE
exec "$app_dir/node_modules/electron/dist/electron" "$app_dir" --class=gpt-manager "$@"
'''
        atomic_text(self.bin / 'gpt-manager', launcher, 0o755)
        uninstall = f'#!/usr/bin/env bash\n# {self.root}\nexec {shlex.quote(sys.executable)} {shlex.quote(str(current / "tools/linux_install.py"))} uninstall --home {shlex.quote(str(self.home))} --data-home {shlex.quote(str(self.data_home))} "$@"\n'
        atomic_text(self.bin / 'gpt-manager-uninstall', uninstall, 0o755)
        for icon in self.icons:
            icon.parent.mkdir(parents=True, exist_ok=True)
            temporary = icon.with_name(icon.name + '.tmp-' + uuid.uuid4().hex)
            try:
                shutil.copyfile(stage / 'ui/assets' / ('icon' + icon.suffix), temporary)
                temporary.chmod(0o644); temporary.replace(icon)
            finally: temporary.unlink(missing_ok=True)
        desktop = f'''[Desktop Entry]
Type=Application
Version=1.0
Name=GPT Manager
Comment=Browse, resume and move your AI conversations
Exec={desktop_exec(self.bin / 'gpt-manager')}
TryExec={desktop_value(self.bin / 'gpt-manager')}
Icon={APP_ID}
Terminal=false
Categories=Development;
Keywords=AI;Codex;Claude;Gemini;Antigravity;Chat;
StartupWMClass=gpt-manager
X-GPT-Manager-Install={desktop_value(self.root)}
'''
        atomic_text(self.desktop, desktop)

    def refresh_desktop(self):
        for command in (['update-desktop-database', str(self.desktop.parent)], ['gtk-update-icon-cache', '-f', '-t', str(self.data_home / 'icons/hicolor')]):
            if shutil.which(command[0]): subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def uninstall(self):
        with self.lock():
            if not self.owned(): print('GPT Manager is not installed.'); return
            target = Path('/etc/apparmor.d') / f'gpt-manager-{os.getuid()}'
            if target.exists() and target.read_text().startswith('# ' + APP_ID + '\n'):
                print('Removing GPT Manager’s AppArmor profile (sudo).', flush=True)
                run(['sudo', 'apparmor_parser', '-R', target]); run(['sudo', 'rm', '--', target])
            for path in (self.bin / 'gpt-manager', self.bin / 'gpt-manager-uninstall', self.desktop):
                if path.exists() and self.owns_launcher(path): path.unlink()
            for path in self.icons: path.unlink(missing_ok=True)
            shutil.rmtree(self.root)
            self.refresh_desktop()
            print('GPT Manager uninstalled. Conversations, account credentials, settings and system packages were kept.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['install', 'uninstall'])
    parser.add_argument('--source', type=Path)
    parser.add_argument('--home', type=Path)
    parser.add_argument('--data-home', type=Path)
    parser.add_argument('--allow-system-changes', action='store_true')
    args = parser.parse_args()
    if sys.platform != 'linux' or os.getuid() == 0: parser.error('Run as your Linux desktop user, without sudo.')
    installer = Installer(args.home, args.data_home)
    try:
        if args.action == 'install':
            if not args.source: parser.error('--source is required for install')
            installer.install(args.source, args.allow_system_changes)
        else: installer.uninstall()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f'Installation stopped: {error}', file=sys.stderr); return 1
    return 0


if __name__ == '__main__': sys.exit(main())
