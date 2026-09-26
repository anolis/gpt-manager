"""Private line-delimited RPC over a child-process pipe, with no listening port."""
import argparse
import json
import sys
import threading
import signal
from pathlib import Path

from manager import Manager
from cloud import CloudSync
from relocate import project_folder, plan_move, move_files

p = argparse.ArgumentParser()
p.add_argument('--data', required=True)
p.add_argument('--home', default=str(Path.home()))
args = p.parse_args()
manager = Manager(args.home, args.data)
manager_lock = threading.RLock()
cloud = CloudSync(manager, manager_lock)
signal.signal(signal.SIGTERM, lambda *_: (cloud.close(), sys.exit(0)))
cloud_methods = {f'cloud_{name}': getattr(cloud, name) for name in ('status', 'connect', 'answer', 'cancel', 'folder', 'configure', 'configure_google', 'disconnect', 'sync', 'tick')}
methods = {name: getattr(manager, name) for name in ('scan', 'library', 'detail', 'annotate', 'add_root', 'files', 'export', 'preview', 'import_archive', 'restore')}
methods.update({'project_folder': lambda **params: project_folder(manager, **params), 'plan_move': lambda **params: plan_move(manager, **params), 'move_files': lambda **params: move_files(manager, **params)})
for line in sys.stdin:
    request = {}
    try:
        request = json.loads(line)
        method = methods.get(request.get('method')) or cloud_methods.get(request.get('method'))
        if method is None:
            raise ValueError('Unknown operation')
        if request.get('method') in cloud_methods:
            result = method(**request.get('params', {}))
        else:
            if request.get('method') in ('move_files', 'project_folder') and cloud.job and cloud.job['status'] == 'running':
                raise ValueError('Wait for the cloud operation to finish before moving this context')
            with manager_lock:
                result = method(**request.get('params', {}))
        if request.get('method') == 'move_files':
            cloud.remap_context(result['previousId'], result['id'])
        response = {'id': request['id'], 'result': result}
    except Exception as e:
        response = {'id': request.get('id'), 'error': str(e)}
    print(json.dumps(response, ensure_ascii=False), flush=True)

cloud.close()
