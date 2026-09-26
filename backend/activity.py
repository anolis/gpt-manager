"""Timestamp-based, bounded activity excerpts. No model calls or filesystem writes."""
import json
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

if 'message' not in globals():
    try:
        from .manager import message
    except ImportError:
        from manager import message

ACTIVITY_BYTES = 4 * 1024**2
EXCERPT_CHARS = 6000


def parse_time(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result.astimezone(timezone.utc) if result.tzinfo is not None else None
    except ValueError:
        return None


def time_window(start, end):
    lower, upper = parse_time(start), parse_time(end)
    if lower is None or upper is None or upper <= lower or (upper - lower).total_seconds() > 93 * 86400:
        raise ValueError('Choose a valid date range of at most 93 days, including its timezone.')
    return lower, upper


def activity_excerpt(context, start, end):
    lower, upper = time_window(start, end)
    if not context.get('readable'):
        return {'messages': [], 'bytesRead': 0, 'partial': False, 'undated': 0, 'matched': 0, 'unreadable': True}
    path = Path(context['path'])
    size = path.stat().st_size
    if path.suffix == '.json':
        if size > ACTIVITY_BYTES:
            return {'messages': [], 'bytesRead': 0, 'partial': True, 'undated': 0, 'matched': 0, 'unreadable': True}
        data = json.loads(path.read_bytes())
        rows = data.get('messages', []) if isinstance(data, dict) else []
        if not isinstance(rows, list):
            rows = []
        read = size
        partial = False
    else:
        with path.open('rb') as stream:
            stream.seek(max(0, size - ACTIVITY_BYTES))
            data = stream.read(ACTIVITY_BYTES)
        read = len(data)
        partial = size > ACTIVITY_BYTES
        if partial:
            # The tail can start in the middle of a UTF-8 character or JSON record.
            data = data.partition(b'\n')[2]
        rows = []
        for line in data.splitlines():
            try:
                rows.append(json.loads(line))
            except (ValueError, UnicodeDecodeError):
                partial = True
    recent = deque()
    chars = undated = matched = 0
    first = last = None
    for row in rows:
        try:
            normalized = message(row, context['provider'])
        except (ValueError, TypeError, AttributeError):
            partial = True
            continue
        if not normalized or normalized['role'] not in ('user', 'assistant'):
            continue
        timestamp = parse_time(normalized['timestamp'])
        if timestamp is None:
            undated += 1
            continue
        if not lower <= timestamp < upper:
            continue
        matched += 1
        stamp = timestamp.isoformat()
        first = min(first, stamp) if first else stamp
        last = max(last, stamp) if last else stamp
        content = normalized['content']
        # Keep both request/context and conclusions when a message is long.
        if len(content) > 1800:
            content = content[:900] + '\n[excerpt truncated]\n' + content[-900:]
            partial = True
        item = {'role': normalized['role'], 'timestamp': stamp, 'content': content}
        recent.append(item); chars += len(content)
        while chars > EXCERPT_CHARS and len(recent) > 1:
            chars -= len(recent.popleft()['content']); partial = True
    return {'messages': sorted(recent, key=lambda x: x['timestamp']), 'bytesRead': read, 'partial': partial, 'undated': undated, 'matched': matched, 'first': first, 'last': last}
