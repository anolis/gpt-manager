"""Private line-delimited RPC over a child-process pipe, with no listening port."""
import argparse
import json
import sys
import threading
import signal
from pathlib import Path

sys.stdin.reconfigure(encoding='utf-8')
sys.stdout.reconfigure(encoding='utf-8')

from manager import Manager
from cloud import CloudSync
from relocate import project_folder, plan_move, move_files
from remote import RemoteLocations, ssh_aliases
from teleport import Teleport, git_check
from network import local_networks, scan_network
from catchup import CatchUp
from providers import ProviderSetup
from recap_schedule import RecapSchedule
from usage import Usage

p = argparse.ArgumentParser()
p.add_argument('--data', required=True)
p.add_argument('--home', default=str(Path.home()))
args = p.parse_args()
manager = Manager(args.home, args.data)
manager_lock = threading.RLock()
cloud = CloudSync(manager, manager_lock)
remote = RemoteLocations(manager, progress=lambda message: print(json.dumps({'event': 'handoffProgress', 'message': message}), flush=True))
teleport = Teleport(remote)
setup = ProviderSetup(manager.data)
catchup = CatchUp(remote, manager_lock, setup)
usage = Usage(manager.data, setup)
schedule = RecapSchedule(catchup)
schedule.start()
signal.signal(signal.SIGTERM, lambda *_: (usage.close(), schedule.close(), setup.close(), catchup.close(), cloud.close(), sys.exit(0)))
cloud_methods = {f'cloud_{name}': getattr(cloud, name) for name in ('status', 'connect', 'answer', 'cancel', 'folder', 'configure', 'configure_google', 'disconnect', 'sync', 'tick')}
setup_methods = {'setup_status': setup.status, 'setup_install': setup.install, 'setup_cancel': setup.cancel, 'setup_prefer': setup.prefer, 'setup_auth_refresh': setup.auth_refresh, 'setup_uninstall': setup.uninstall, 'usage_status': usage.status, 'usage_refresh': usage.refresh, 'usage_configure': usage.configure, 'provider_command': lambda provider: {'argv': setup.command(provider), 'env': setup.environment()}}
catchup_methods = {f'catchup_{name}': getattr(catchup, name) for name in ('status', 'prepare', 'generate', 'cancel', 'history', 'get', 'delete')}
catchup_methods.update({'catchup_schedule_status': schedule.status, 'catchup_schedule_configure': schedule.configure})
methods = {name: getattr(manager, name) for name in ('scan', 'library', 'detail', 'annotate', 'add_root', 'files', 'export', 'preview', 'import_archive', 'restore')}
methods.update({'project_folder': lambda **params: project_folder(manager, **params), 'plan_move': lambda **params: plan_move(manager, **params), 'move_files': lambda **params: move_files(manager, **params)})
methods.update({'scan': remote.scan, 'library': remote.library,
                'detail': lambda **params: remote.read('detail', **params),
                'files': lambda **params: remote.read('files', **params),
                'ssh_add': remote.add, 'ssh_remove': remote.remove, 'ssh_refresh': remote.refresh,
                'ssh_resume': remote.resume, 'ssh_aliases': ssh_aliases,
                'handoff_release': remote.release, 'teleport': teleport.run, 'git_check': git_check,
                'local_networks': local_networks, 'scan_network': scan_network})
for line in sys.stdin:
    request = {}
    try:
        request = json.loads(line)
        method = methods.get(request.get('method')) or cloud_methods.get(request.get('method')) or catchup_methods.get(request.get('method')) or setup_methods.get(request.get('method'))
        if method is None:
            raise ValueError('Unknown operation')
        if request.get('method') in cloud_methods or request.get('method') in catchup_methods or request.get('method') in setup_methods:
            result = method(**request.get('params', {}))
        else:
            if request.get('method') in ('move_files', 'project_folder') and cloud.job and cloud.job['status'] == 'running':
                raise ValueError('Wait for the cloud operation to finish before moving this context')
            with manager_lock:
                result = method(**request.get('params', {}))
        if request.get('method') == 'move_files':
            cloud.remap_context(result['previousId'], result['id'])
        if isinstance(result, dict) and 'contexts' in result and 'locations' in result:
            result = remote.library()
        response = {'id': request['id'], 'result': result}
    except Exception as e:
        response = {'id': request.get('id'), 'error': str(e)}
    print(json.dumps(response, ensure_ascii=False), flush=True)

usage.close()
schedule.close()
setup.close()
catchup.close()
cloud.close()
