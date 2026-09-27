import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from backend.usage import Usage, codex_windows, claude_windows, claude_account_windows, antigravity_windows, UsageUnavailable, NoAuthRedirect
from backend.providers import ProviderSetup
from unittest.mock import patch


class UsageTests(unittest.TestCase):
    def test_claude_account_windows_parse_percentages_and_iso_resets(self):
        values = claude_account_windows({'five_hour': {'utilization': 25, 'resets_at': None}, 'seven_day': {'utilization': 80, 'resets_at': '2026-09-28T00:00:00Z'}, 'extra_usage': {'utilization': None}})
        self.assertEqual([v['remaining'] for v in values], [75, 20])
        self.assertIsNone(values[0]['resetsAt']); self.assertEqual(values[1]['resetsAt'], 1790553600)

    def test_antigravity_report_ignores_missing_disabled_and_invalid_buckets(self):
        value = {'num_turns': 0, 'command': {'name': 'usage', 'data': {'groups': [{'name': 'Gemini', 'buckets': [
            {'name': 'Weekly', 'remaining_fraction': .75, 'reset_time': '2026-09-28T00:00:00Z'},
            {'name': 'Disabled', 'remaining_fraction': 1, 'disabled': True},
            {'name': 'Unknown'}, {'name': 'Bad', 'remaining_fraction': -1}]}]}}}
        result = antigravity_windows(value)
        self.assertEqual(len(result), 1); self.assertEqual(result[0]['remaining'], 75)
        self.assertEqual(result[0]['resetsAt'], 1790553600)
        value['num_turns'] = 1
        with self.assertRaises(ValueError): antigravity_windows(value)

    def test_antigravity_old_cli_never_receives_a_quota_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            setup = ProviderSetup(directory); usage = Usage(directory, setup)
            try:
                with patch.object(setup, 'command', return_value=['agy']), patch.object(usage, '_cli_output', return_value='1.1.10') as read:
                    with self.assertRaises(UsageUnavailable): usage._antigravity_read()
                    self.assertEqual(read.call_count, 1); self.assertEqual(read.call_args.args[0], ['agy', '--version'])
                with patch.object(setup, 'command', return_value=['agy']), patch.object(usage, '_cli_output', side_effect=['1.2.11', '{"num_turns":0}']) as read:
                    self.assertEqual(usage._antigravity_read()['num_turns'], 0)
                    self.assertEqual(read.call_args.args[0], ['agy', '-p', '/usage', '--output-format', 'json'])
            finally: usage.close()

    def test_claude_read_uses_only_fixed_origin_and_redacts_http_errors(self):
        import io
        from urllib.error import HTTPError
        with tempfile.TemporaryDirectory() as directory:
            setup = ProviderSetup(directory); usage = Usage(directory, setup)
            credentials = Path(directory)/'.credentials.json'
            credentials.write_text(json.dumps({'claudeAiOauth': {'accessToken': 'PRIVATE'}}))
            try:
                with patch.object(setup, 'environment', return_value={'CLAUDE_CONFIG_DIR': directory}), patch('backend.usage.build_opener') as opener:
                    opener.return_value.open.return_value.__enter__.return_value.read.return_value = b'{"five_hour":{"utilization":5}}'
                    self.assertEqual(usage._claude_read()['five_hour']['utilization'], 5)
                    request = opener.return_value.open.call_args.args[0]
                    self.assertEqual(request.full_url, 'https://api.anthropic.com/api/oauth/usage')
                    self.assertEqual(request.headers['Authorization'], 'Bearer PRIVATE')
                    opener.return_value.open.side_effect = HTTPError(request.full_url, 401, 'PRIVATE', {}, io.BytesIO(b'PRIVATE'))
                    with self.assertRaises(UsageUnavailable) as error: usage._claude_read()
                    self.assertNotIn('PRIVATE', str(error.exception))
                    opener.return_value.open.side_effect = HTTPError(request.full_url, 429, 'PRIVATE', {'Retry-After': '300'}, io.BytesIO(b''))
                    with self.assertRaises(UsageUnavailable): usage._claude_read()
                    count = opener.return_value.open.call_count
                    with self.assertRaises(UsageUnavailable): usage._claude_read()
                    self.assertEqual(opener.return_value.open.call_count, count)
                self.assertIsNone(NoAuthRedirect().redirect_request(None, None, 302, '', {}, 'https://example.com'))
            finally: usage.close()

    def test_refresh_isolates_provider_failures_and_keeps_last_success(self):
        import time
        with tempfile.TemporaryDirectory() as directory:
            setup = ProviderSetup(directory); usage = Usage(directory, setup)
            usage.antigravity.update(windows=[{'remaining': 42}], updated=123)
            try:
                with patch.object(usage, '_codex_read', side_effect=ValueError('PRIVATE')), patch.object(usage, '_claude_read', return_value={'five_hour': {'utilization': 25}}), patch.object(usage, '_antigravity_read', side_effect=ValueError('PRIVATE')):
                    usage.refresh()
                    for _ in range(100):
                        if not usage.running: break
                        time.sleep(.01)
                self.assertFalse(usage.running)
                status = usage.status(); self.assertNotIn('PRIVATE', json.dumps(status))
                self.assertEqual(status['providers'][1]['windows'][0]['remaining'], 75)
                self.assertEqual(status['providers'][3]['windows'][0]['remaining'], 42)
            finally: usage.close()

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
