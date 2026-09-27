"""Explicit, per-user provider installs. No administrator or global npm changes."""
import copy
import hashlib
import json
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path
from urllib.request import urlopen

try:
    from .manager import atomic_json, read_json
except ImportError:
    from manager import atomic_json, read_json

PACKAGES = {'codex': '@openai/codex', 'claude': '@anthropic-ai/claude-code', 'gemini': '@google/gemini-cli'}
NODE_VERSION = 'v24.18.0'
# Official release checksums, pinned with the runtime version.
NODE_HASHES = {'darwin-arm64': 'e1a97e14c99c803e96c7339403282ea05a499c32f8d83defe9ef5ec66f979ed1', 'darwin-x64': 'dfd0dbd3e721503434df7b7205e719f61b3a3a31b2bcf9729b8b91fea240f080', 'linux-arm64': '6b4484c2190274175df9aa8f28e2d758a819cb1c1fe6ab481e2f95b463ab8508', 'linux-x64': '783130984963db7ba9cbd01089eaf2c2efb055c7c1693c943174b967b3050cb8', 'win-arm64': 'f274669adb93b1fd0fbf8f21fd078609e9dcc84333d4f2718d2dde3f9a161a01', 'win-x64': '0ae68406b42d7725661da979b1403ec9926da205c6770827f33aac9d8f26e821'}


def stop_process(process):
    if not process or process.poll() is not None:
        return
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True, timeout=10)
    else:
        import signal
        try: os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError: return
    try: process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        if os.name != 'nt':
            import signal
            try: os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError: pass
        else: process.kill()
        process.wait(timeout=3)


class ProviderSetup:
    def __init__(self, data):
        self.root = Path(data) / 'providers'
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.job = None
        self.cancel_event = threading.Event()
        self.process = None
        self.thread = None

    def runtime(self):
        system = {'Windows': 'win', 'Linux': 'linux', 'Darwin': 'darwin'}.get(platform.system())
        arch = {'AMD64': 'x64', 'x86_64': 'x64', 'arm64': 'arm64', 'aarch64': 'arm64'}.get(platform.machine())
        key = f'{system}-{arch}'
        if key not in NODE_HASHES:
            raise ValueError('Automatic installation supports Windows, macOS and glibc Linux on x64/ARM64.')
        folder = self.root / f'node-{NODE_VERSION}-{key}'
        node = folder / ('node.exe' if system == 'win' else 'bin/node')
        npm = folder / ('node_modules/npm/bin/npm-cli.js' if system == 'win' else 'lib/node_modules/npm/bin/npm-cli.js')
        return key, folder, node, npm

    def environment(self):
        env = dict(os.environ)
        try:
            _, _, node, _ = self.runtime()
            if node.exists(): env['PATH'] = str(node.parent) + os.pathsep + env.get('PATH', '')
        except ValueError: pass
        env.pop('ELECTRON_RUN_AS_NODE', None)
        return env

    def package_command(self, folder, provider):
        package = folder / 'node_modules' / PACKAGES[provider]
        metadata = read_json(package / 'package.json', {})
        entry = metadata.get('bin', {})
        if isinstance(entry, dict): entry = entry.get(provider)
        if not isinstance(entry, str): raise ValueError('Provider package has no supported executable.')
        binary = (package / entry).resolve()
        if not binary.is_relative_to(folder.resolve()) or not binary.is_file():
            raise ValueError('Provider executable is missing or outside its installation.')
        if binary.suffix in ('.js', '.mjs', '.cjs'):
            node = self.runtime()[2]
            return [str(node) if node.is_file() else shutil.which('node') or str(node), str(binary)]
        return [str(binary)]

    def managed_command(self, provider):
        record = read_json(self.root / f'{provider}.json', {})
        folder = record.get('folder', '')
        if folder and Path(folder).name == folder:
            try: return self.package_command(self.root / folder, provider)
            except (ValueError, OSError, KeyError): pass
        return None

    def existing_command(self, provider):
        name = 'agy' if provider == 'antigravity' else provider
        binary = shutil.which(name)
        if binary and Path(binary).suffix.lower() not in ('.cmd', '.bat', '.ps1'):
            return [binary]
        if binary and provider in PACKAGES:
            try: return self.package_command(Path(binary).parent, provider)
            except (ValueError, OSError): pass
        native = Path.home() / '.local/bin' / (name + ('.exe' if os.name == 'nt' else ''))
        if native.is_file(): return [str(native)]
        return None

    def command(self, provider):
        if provider not in (*PACKAGES, 'antigravity'): raise ValueError('Unknown provider')
        preference = read_json(self.root / 'preferences.json', {}).get(provider, 'existing')
        managed = self.managed_command(provider)
        command = managed if preference == 'managed' else self.existing_command(provider) or managed
        if command: return command
        raise ValueError('Open AI setup to install this provider, then sign in.')

    def prefer(self, provider, source):
        if provider not in PACKAGES or source not in ('existing', 'managed'): raise ValueError('Choose an installed provider source.')
        if source == 'managed' and not self.managed_command(provider): raise ValueError('Install a managed copy first.')
        if source == 'existing' and not self.existing_command(provider): raise ValueError('No existing CLI was found. Install one and refresh, or use a managed copy.')
        with self.lock:
            preferences = read_json(self.root / 'preferences.json', {})
            preferences[provider] = source
            atomic_json(self.root / 'preferences.json', preferences)
        return self.status()

    def status(self):
        providers = []
        for provider in PACKAGES:
            try: command = self.command(provider)
            except ValueError: command = None
            record = read_json(self.root / f'{provider}.json', {})
            existing = self.existing_command(provider); managed = self.managed_command(provider)
            providers.append({'id': provider, 'available': bool(command), 'managed': bool(managed and command == managed), 'version': record.get('version', '') if command == managed else '', 'existingAvailable': bool(existing), 'managedAvailable': bool(managed), 'executable': command[-1] if command else ''})
        with self.lock: return {'providers': providers, 'job': copy.deepcopy(self.job)}

    def _check(self):
        if self.cancel_event.is_set(): raise InterruptedError('Installation canceled. You can retry when ready.')

    def _progress(self, message, percent=None):
        with self.lock: self.job.update(message=message, percent=percent)

    def install(self, provider):
        if provider not in PACKAGES: raise ValueError('Choose Codex, Claude or Gemini CLI.')
        with self.lock:
            if self.job and self.job['status'] == 'running': raise ValueError('Wait for the current installation or cancel it.')
            self.cancel_event.clear()
            self.job = {'provider': provider, 'status': 'running', 'message': 'Preparing installation…', 'percent': None}
            self.thread = threading.Thread(target=self._install, args=(provider,), daemon=True)
            self.thread.start()
        return self.status()

    def _download_runtime(self):
        key, destination, node, npm = self.runtime()
        if node.is_file() and npm.is_file(): return
        extension = 'zip' if key.startswith('win-') else 'tar.gz'
        filename = destination.name + '.' + extension
        with tempfile.TemporaryDirectory(dir=self.root, prefix='runtime-') as temporary:
            archive = Path(temporary) / filename
            digest = hashlib.sha256()
            self._progress('Downloading the runtime…', 0)
            with urlopen('https://nodejs.org/dist/' + NODE_VERSION + '/' + filename, timeout=30) as response, archive.open('wb') as out:
                total = int(response.headers.get('Content-Length', 0)); received = 0
                while True:
                    self._check()
                    chunk = response.read(256 * 1024)
                    if not chunk: break
                    received += len(chunk)
                    if received > 150 * 1024**2: raise ValueError('Runtime download exceeded its size limit.')
                    out.write(chunk); digest.update(chunk)
                    self._progress(f'Downloading runtime: {received // 1024**2} MB' + (f' of {total // 1024**2} MB' if total else ''), round(received / total * 100) if total else None)
            if digest.hexdigest() != NODE_HASHES[key]: raise ValueError('Runtime checksum did not match. Retry the download.')
            self._check(); self._progress('Verified download. Unpacking the runtime…')
            if extension == 'zip':
                with zipfile.ZipFile(archive) as bundle:
                    for item in bundle.infolist():
                        if not (Path(temporary) / item.filename).resolve().is_relative_to(Path(temporary).resolve()): raise ValueError('Unsafe runtime archive')
                    bundle.extractall(temporary)
            else:
                with tarfile.open(archive) as bundle: bundle.extractall(temporary, filter='data')
            self._check()
            if destination.exists(): shutil.rmtree(destination)
            (Path(temporary) / destination.name).rename(destination)

    def _run(self, args, cwd, timeout=600):
        with tempfile.TemporaryFile() as log:
            env = self.environment()
            # Do not inherit private registries, global prefixes, or npm startup settings.
            env = {key: value for key, value in env.items() if not key.lower().startswith('npm_config_')}
            env.update(NPM_CONFIG_USERCONFIG=str(self.root / 'empty.npmrc'), NPM_CONFIG_GLOBALCONFIG=str(self.root / 'empty-global.npmrc'), NPM_CONFIG_CACHE=str(self.root / 'npm-cache'))
            with self.lock:
                self._check()
                self.process = subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=os.name != 'nt', creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                process = self.process
            deadline = time.monotonic() + timeout
            try:
                while process.poll() is None:
                    self._check()
                    if time.monotonic() > deadline: raise ValueError('Installation timed out. Check your connection and retry.')
                    if os.fstat(log.fileno()).st_size > 8 * 1024**2: raise ValueError('Installer output exceeded its limit.')
                    self.cancel_event.wait(0.2)
                self._check()
                if process.returncode: raise ValueError('Provider installation or verification failed. Check your connection and available disk space, then retry.')
                log.seek(0); return log.read(4096).decode('utf-8', errors='replace').strip()
            finally:
                stop_process(process)
                with self.lock: self.process = None

    def _install(self, provider):
        stage = None
        try:
            self._download_runtime(); self._check()
            stage = self.root / (provider + '-' + uuid.uuid4().hex); stage.mkdir()
            self._progress(f'Installing {provider.capitalize()} and its dependencies… This can take several minutes.')
            (stage / 'package.json').write_text('{"private":true}')
            _, _, node, npm = self.runtime()
            self._run([str(node), str(npm), 'install', '--prefix', str(stage), '--registry=https://registry.npmjs.org', '--no-audit', '--no-fund', '--save-exact', '--include=optional', PACKAGES[provider] + '@latest'], stage)
            self._progress('Checking the installed CLI…')
            command = self.package_command(stage, provider)
            self._run([*command, '--version'], stage, timeout=45)
            metadata = read_json(stage / 'node_modules' / PACKAGES[provider] / 'package.json', {})
            with self.lock:
                self._check()
                atomic_json(self.root / f'{provider}.json', {'folder': stage.name, 'version': metadata.get('version', '')})
                preferences = read_json(self.root / 'preferences.json', {})
                preferences[provider] = 'managed'
                atomic_json(self.root / 'preferences.json', preferences)
                stage = None  # Keep older versions too: a running terminal may still use one.
                self.job.update(status='complete', message='Installed. Choose Sign in to connect your account.', percent=100)
        except InterruptedError as error:
            with self.lock: self.job.update(status='canceled', message=str(error), percent=None)
        except Exception as error:
            with self.lock: self.job.update(status='error', message=str(error)[:500], percent=None)
        finally:
            if stage: shutil.rmtree(stage, ignore_errors=True)

    def cancel(self):
        with self.lock: self.cancel_event.set()
        return self.status()

    def close(self):
        self.cancel_event.set()
        with self.lock: process = self.process
        stop_process(process)
