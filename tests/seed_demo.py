"""Synthetic contexts for desktop smoke tests; never points at live provider stores."""
import json
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
project = root / 'projects/observatory'
project.mkdir(parents=True, exist_ok=True)
(root / '.gpt-manager-smoke-fixture').write_text('Synthetic GPT Manager smoke fixtures\n')
contexts = [
    ('codex', 'Chart a path through the night sky', 'observatory'),
    ('claude', 'Review the telescope control service', 'telescope'),
    ('gemini', 'Design the observation dashboard', 'dashboard'),
    ('antigravity', 'Build a portable context library', 'context-library'),
]
for provider, title, name in contexts:
    if provider == 'codex':
        path = root / '.codex/sessions/2026/09/25/rollout-observatory.jsonl'
        data = [
            {'type': 'session_meta', 'payload': {'id': 'demo-codex-session', 'cwd': str(project)}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [{'text': title + '. Start with a simple overview of the observation plan.'}]}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant', 'content': [{'text': 'We can organize the session into three parts:\n\n1. Check visibility and weather.\n2. Select a target and calibrate the telescope.\n3. Capture observations and save the results.\n\nThe project already has a scheduler, so I’ll begin by inspecting how it selects targets.'}]}},
        ]
    elif provider == 'claude':
        path = root / '.claude/projects/observatory/demo-claude.jsonl'
        data = [{'sessionId': 'demo-claude', 'cwd': str(project), 'type': 'user', 'message': {'role': 'user', 'content': title}}, {'type': 'assistant', 'message': {'role': 'assistant', 'content': 'The control service is ready for review.'}}]
    elif provider == 'gemini':
        path = root / '.gemini/tmp/project/chats/session-demo.json'
        data = {'sessionId': 'demo-gemini', 'cwd': str(project), 'messages': [{'type': 'user', 'content': title}, {'type': 'gemini', 'content': 'Let us start with the observation timeline.'}]}
    else:
        path = root / '.gemini/antigravity-cli/brain/demo-agy/.system_generated/logs/transcript.jsonl'
        data = [{'type': 'USER_INPUT', 'content': title}, {'type': 'PLANNER_RESPONSE', 'content': 'I will map the provider stores and preserve their original files.'}]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data) if isinstance(data, dict) else ''.join(json.dumps(x) + '\n' for x in data))
# A fake interactive provider verifies PTY wiring without resuming or modifying real conversations.
bin_dir = root / 'bin'; bin_dir.mkdir(exist_ok=True)
cli = bin_dir / 'codex'
cli.write_text('#!/usr/bin/env python3\nimport os,sys\nprint("PTY_READY " + os.getcwd() + " " + " ".join(sys.argv[1:]), flush=True)\nfor line in sys.stdin:\n print("ECHO:"+line.strip(),flush=True)\n if line.strip()=="exit": break\n')
cli.chmod(0o700)
