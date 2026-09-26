"""Explicit integration test for real rclone RPC, using local alias remotes only.
Run with GPT_MANAGER_RCLONE=/path/to/verified/rclone python3 tests/cloud_connector_smoke.py.
"""
import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.manager import Manager
from backend.cloud import CloudSync, Connector

if not os.environ.get('GPT_MANAGER_RCLONE'):
    raise SystemExit('Set GPT_MANAGER_RCLONE to the verified connector executable')

with tempfile.TemporaryDirectory(prefix='gpt-manager-cloud-test-') as temp:
    root = Path(temp)
    home = root / 'home'; home.mkdir()
    transcript = home / '.codex/sessions/fixture.jsonl'; transcript.parent.mkdir(parents=True)
    transcript.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': 'fixture-session', 'cwd': str(home)}}) + '\n' + json.dumps({'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [{'text': 'Cloud connector integration fixture'}]}}) + '\n')
    source = Manager(home, root / 'manager-a'); source.scan()
    other = Manager(root / 'empty', root / 'manager-b'); other.scan()
    connector = Connector(source.data / 'cloud')
    a = CloudSync(source, connector=connector); b = CloudSync(other)
    try:
        bucket = root / 'local-remote'; bucket.mkdir()
        id = uuid.uuid4().hex
        connector.call('config/create', {'name': 'gpt_' + id, 'type': 'alias', 'parameters': {'remote': str(bucket)}, 'opt': {'nonInteractive': True}})
        connector.call('operations/mkdir', {'fs': 'gpt_' + id + ':', 'remote': 'GPT Manager/v1'})
        a.state['targets'].append({'id': id, 'provider': 'onedrive', 'label': 'Local transport fixture', 'mode': 'account', 'selection': list(source.contexts), 'allLocal': False, 'auto': False, 'fingerprints': {}})
        target_b = b.folder('onedrive', bucket)['targets'][0]['id']
        def wait(cloud):
            for _ in range(600):
                status = cloud.status()
                if status['job']['status'] != 'running':
                    assert status['job']['status'] == 'complete', status
                    return status['job']['result']
                time.sleep(.1)
            raise RuntimeError('Timed out')
        a.sync(id); pushed = wait(a)
        b.sync(target_b); pulled = wait(b)
        assert pushed['exported'] == 1 and pulled['imported'] == 1
        item = next(iter(other.contexts.values()))
        assert other.detail(item['id'])['messages'][0]['content'] == 'Cloud connector integration fixture'
        a.sync(id); repeat = wait(a)
        assert repeat['exported'] == 0
        print(json.dumps({'realConnector': True, 'uploaded': pushed['exported'], 'imported': pulled['imported'], 'repeatUpload': repeat['exported']}))
    finally:
        a.close(); b.close()
