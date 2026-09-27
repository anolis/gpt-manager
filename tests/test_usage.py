import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from backend.usage import Usage, codex_windows, claude_windows
from backend.providers import ProviderSetup
from unittest.mock import patch


class UsageTests(unittest.TestCase):
    def test_windows_preserve_provider_resets_and_remaining(self):
        value = {'rateLimitsByLimitId': {'shared': {'primary':{'usedPercent':25,'windowDurationMins':300,'resetsAt':1234},'secondary':{'usedPercent':100,'windowDurationMins':10080,'resetsAt':5678}}}}
        result = codex_windows(value)
        self.assertEqual([x['remaining'] for x in result],[75,0]); self.assertEqual(result[0]['resetsAt'],1234)
        self.assertIn('5 hour',result[0]['label']); self.assertIn('7 day',result[1]['label'])
    def test_absent_or_invalid_quota_is_not_zero_usage(self):
        self.assertEqual(codex_windows({}),[])
        self.assertEqual(claude_windows({'five_hour':{'used_percentage':None}}),[])
        self.assertEqual(claude_windows({'five_hour':{'used_percentage':float('nan')}}),[])
        self.assertEqual(claude_windows({'spend_limit':{'used_percentage':120}})[0]['remaining'],0)
    def test_statusline_capture_writes_only_quota_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/'usage.json'
            value = {'rate_limits':{'five_hour':{'used_percentage':23.5,'resets_at':1234}},'api_key':'PRIVATE','transcript_path':'PRIVATE','prompt':'PRIVATE'}
            result = subprocess.run([sys.executable,'backend/usage_capture.py',str(path),'fixture-store'],input=json.dumps(value),text=True,capture_output=True,check=True)
            self.assertIn('76.5%',result.stdout)
            data = path.read_text(); self.assertNotIn('PRIVATE',data); self.assertEqual(json.loads(data)['source'],'fixture-store')
    def test_codex_protocol_reads_limits_without_starting_a_turn(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); script = root/'cli.py'
            script.write_text('''import sys,json
for line in sys.stdin:
    value=json.loads(line)
    method=value['method']
    assert method in ('initialize','initialized','account/rateLimits/read')
    if method=='initialize': print(json.dumps({'id':value['id'],'result':{}}),flush=True)
    if method=='account/rateLimits/read': print(json.dumps({'id':value['id'],'result':{'rateLimits':{'primary':{'usedPercent':12,'resetsAt':1234}}}}),flush=True)
''')
            setup = ProviderSetup(root); usage = Usage(root,setup)
            try:
                with patch.object(setup,'command',return_value=[sys.executable,str(script)]):
                    value = usage._codex_read()
                self.assertEqual(codex_windows(value)[0]['remaining'],88)
            finally: usage.close()
