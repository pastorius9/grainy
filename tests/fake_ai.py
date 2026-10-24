"""Offline CLI peer: event formats, split UTF-8 writes and a child for Stop tests."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

kind, mode = sys.argv[1:3]
if mode == 'tree':
    child = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(120)'])
    Path(sys.argv[3]).write_text(str(child.pid))
    time.sleep(120)
elif mode == 'stdin':
    assert sys.stdin.read() == ''
    print(json.dumps({'type': 'turn.completed'}), flush=True)
elif mode == 'fail':
    print('secret-key-in-stderr', file=sys.stderr, flush=True)
    sys.exit(7)
elif mode == 'malformed':
    print('not-json\n[]\nnull', flush=True)
    print(json.dumps({'type': 'turn.completed'}), flush=True)
else:
    data = {
        'claude': [
            {'type': 'system', 'subtype': 'init', 'session_id': 'session-1'},
            {'type': 'stream_event', 'event': {'type': 'message_start'}},
            {'type': 'stream_event', 'event': {'delta': {'type': 'text_delta', 'text': '사진 확인'}}},
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': '사진 확인'},
                {'type': 'tool_use', 'id': 't1', 'name': 'mcp__grainy__apply_edits', 'input': {'edits': [{'ids': [1, 2]}]}}]}},
            {'type': 'result', 'is_error': False}],
        'codex': [
            {'type': 'thread.started', 'thread_id': 'session-1'},
            {'type': 'item.started', 'item': {'type': 'mcp_tool_call', 'id': 't1', 'server': 'grainy',
                'tool': 'apply_edits', 'arguments': {'edits': [{'ids': [1, 2]}]}}},
            {'type': 'item.completed', 'item': {'type': 'mcp_tool_call', 'id': 't1', 'server': 'grainy', 'tool': 'apply_edits'}},
            {'type': 'item.completed', 'item': {'type': 'agent_message', 'id': 'a1', 'text': '사진 확인'}},
            {'type': 'turn.completed'}],
        'gemini': [
            {'type': 'init', 'session_id': 'session-1'},
            {'type': 'message', 'role': 'assistant', 'content': '사진 확인', 'delta': True},
            {'type': 'tool_use', 'tool_id': 't1', 'tool_name': 'apply_edits', 'parameters': {'edits': [{'ids': [1, 2]}]}},
            {'type': 'result', 'status': 'success'}],
    }[kind]
    for event in data:
        raw = (json.dumps(event, ensure_ascii=False) + '\n').encode('utf-8')
        for piece in (raw[:7], raw[7:]):
            os.write(sys.stdout.fileno(), piece)
            time.sleep(.01)
