"""Claude status-line adapter. Store only quota percentages/reset times, never stdin."""
import json
import os
import sys
import time
import uuid
from pathlib import Path
try:
    from .usage import claude_windows
except ImportError:
    from usage import claude_windows


def main():
    destination, source = sys.argv[1:3]
    try:
        raw = sys.stdin.buffer.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024: return
        windows = claude_windows(json.loads(raw).get('rate_limits') or {})
        # Missing limits do not manufacture an unused allowance.
        if not windows:
            print('GPT Manager · usage not reported'); return
        path = Path(destination); path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.tmp-' + uuid.uuid4().hex)
        try:
            temporary.write_text(json.dumps({'windows': windows, 'updated': time.time(), 'source': source}), encoding='utf-8')
            temporary.chmod(0o600); temporary.replace(path)
        finally: temporary.unlink(missing_ok=True)
        print(' · '.join(f"{item['label']}: {item['remaining']:g}% left" for item in windows))
    except (OSError, ValueError, TypeError, AttributeError):
        print('GPT Manager · usage unavailable')

if __name__ == '__main__': main()
