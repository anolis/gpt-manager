"""Private line-delimited RPC over a child-process pipe, with no listening port."""
import argparse
import json
import sys
from pathlib import Path

from manager import Manager

p = argparse.ArgumentParser()
p.add_argument('--data', required=True)
p.add_argument('--home', default=str(Path.home()))
args = p.parse_args()
manager = Manager(args.home, args.data)
methods = {name: getattr(manager, name) for name in ('scan', 'library', 'detail', 'annotate', 'add_root', 'files', 'export', 'preview', 'import_archive', 'restore')}
for line in sys.stdin:
    request = {}
    try:
        request = json.loads(line)
        method = methods.get(request.get('method'))
        if method is None:
            raise ValueError('Unknown operation')
        result = method(**request.get('params', {}))
        response = {'id': request['id'], 'result': result}
    except Exception as e:
        response = {'id': request.get('id'), 'error': str(e)}
    print(json.dumps(response, ensure_ascii=False), flush=True)
