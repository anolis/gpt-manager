"""Download/launch official CLIs in isolation; never authenticate or send prompts."""
import sys
import tempfile
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.providers import ProviderSetup

with tempfile.TemporaryDirectory(prefix='gpt-installer-check-') as temporary:
    setup = ProviderSetup(temporary)
    try:
        for provider in ('codex', 'claude', 'gemini', 'antigravity'):
            setup.install(provider)
            while setup.status()['job']['status'] == 'running':
                time.sleep(2)
            result = setup.status()['job']
            print(provider, result, flush=True)
            if result['status'] != 'complete': raise SystemExit(1)
    finally: setup.close()
