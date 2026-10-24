import json
import os
from pathlib import Path
import sys
import time
from dataclasses import replace

import pytest

from luma import ai_programs as ai
from test_studio_ui import app, wait


@pytest.fixture(autouse=True)
def isolated_codex_cache(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'codex-cache'))


def request(tmp_path, key='codex', session=''):
    workspace = tmp_path / 'settings' / 'ai-workspace' / key
    workspace.mkdir(parents=True, exist_ok=True)
    entry = tmp_path / 'npm/gemini/dist/index.js'; entry.parent.mkdir(parents=True, exist_ok=True); entry.touch()
    storage = entry.parent.parent / 'node_modules/@google/gemini-cli-core/dist/src/config/storage.js'
    storage.parent.mkdir(parents=True, exist_ok=True); storage.touch()
    executable = ('node', str(entry)) if key == 'gemini' else ('cli.exe',)
    return ai.Request(executable, workspace, {'command': 'powershell', 'args': ['-NoProfile', '-File', 'bridge.ps1']},
                      '노출을 올려줘 & echo no', model='test-model', session_id=session)


@pytest.mark.parametrize('profile', ai.PROFILES, ids=lambda p: p.key)
def test_commands_are_argument_vectors_with_only_per_run_config(tmp_path, profile):
    r = request(tmp_path, profile.key)
    args = profile.build(r)
    assert all(isinstance(a, str) for a in args)
    assert not any('dangerously' in a for a in args)
    assert 'test-model' in args
    assert not any('mcp add' in a for a in args)
    if profile.key == 'claude':
        assert args[args.index('--tools') + 1] == ''
        assert args[args.index('--permission-mode') + 1] == 'dontAsk'
        assert '--strict-mcp-config' in args and '--no-session-persistence' in args
        config = json.loads((r.workspace / 'claude-mcp.json').read_text())
        assert config == {'mcpServers': {'grainy': r.server}}
    elif profile.key == 'codex':
        assert '--ignore-user-config' in args and '--ephemeral' in args
        assert args[args.index('-s') + 1] == 'read-only'
        for setting in ['features.shell_tool=false', 'features.unified_exec=false', 'web_search="disabled"',
                        'approval_policy="never"', 'mcp_servers.grainy.default_tools_approval_mode="approve"']:
            assert setting in args
    else:
        config = json.loads((r.workspace / '.gemini/settings.json').read_text())
        assert config['tools']['core'] == []
        assert config['tools']['discoveryCommand'] == config['tools']['callCommand'] == ''
        assert config['mcpServers']['grainy']['trust'] is True
        assert config['mcp']['serverCommand'] == ''
        assert args[args.index('--allowed-mcp-server-names') + 1] == 'grainy'
        wrapper = (r.workspace / 'gemini-entry.mjs').read_text()
        assert 'Storage.getGlobalTempDir' in wrapper and 'getHistoryDir' in wrapper
        assert 'auth' not in wrapper and 'settings.json' not in wrapper


@pytest.mark.parametrize('profile', ai.PROFILES, ids=lambda p: p.key)
def test_resume_obeys_recording_boundary(tmp_path, profile):
    args = profile.build(request(tmp_path, profile.key, 'previous-session'))
    if profile.persistent:
        assert args[args.index('--resume') + 1] == 'previous-session'
    else:
        assert 'previous-session' not in args



def test_codex_static_catalog_removes_metadata_tools_without_changing_home(tmp_path, monkeypatch):
    home = tmp_path / 'codex-home'; home.mkdir()
    monkeypatch.setenv('CODEX_HOME', str(home))
    cache = home / 'models_cache.json'
    original = json.dumps({'identity': 'private-account-marker', 'models': [
        {'slug': 'example', 'apply_patch_tool_type': 'freeform', 'shell_type': 'unified_exec',
         'experimental_supported_tools': ['clock', 'test_sync_tool'], 'tool_mode': 'code_mode',
         'multi_agent_version': 'v2'}]})
    cache.write_text(original)
    r = request(tmp_path)
    args = ai.codex_command(r)
    catalog = json.loads((r.workspace / 'codex-models.json').read_text())
    assert cache.read_text() == original
    assert 'private-account-marker' not in json.dumps(catalog)
    assert len(list(home.iterdir())) == 1
    model = catalog['models'][0]
    assert model['apply_patch_tool_type'] is None and model['shell_type'] == 'disabled'
    assert model['experimental_supported_tools'] == [] and model['tool_mode'] is None
    assert 'agents.enabled=false' in args
    assert 'tools.experimental_request_user_input.enabled=false' in args
    cache.unlink()
    assert json.loads(ai.codex_catalog(r).read_text()) == {'models': []}
    r.model = ''
    with pytest.raises(ValueError, match='model cache'):
        ai.codex_catalog(r)

def test_npm_detection_bypasses_shell_and_requires_node(tmp_path, monkeypatch):
    script = tmp_path / 'node_modules/@google/gemini-cli/dist/index.js'
    script.parent.mkdir(parents=True); script.touch()
    found = {'gemini': str(tmp_path / 'gemini.cmd'), 'node': 'node.exe'}
    monkeypatch.setattr(ai.shutil, 'which', found.get)
    if os.name == 'nt':
        assert ai.find_program('gemini') == ('node.exe', str(script))
        found.pop('node'); assert ai.find_program('gemini') is None
    monkeypatch.setattr(ai.Path, 'home', lambda: tmp_path)
    assert ai.find_program('claude') is None


@pytest.mark.parametrize('profile', ai.PROFILES, ids=lambda p: p.key)
def test_streaming_peer_and_tool_deduplication(app, tmp_path, profile):
    peer = Path(__file__).with_name('fake_ai.py')
    profile = replace(profile, build=lambda r: [sys.executable, str(peer), profile.key, 'stream'])
    runner = ai.AiRunner(); events = []; results = []
    runner.event.connect(events.append); runner.finished.connect(results.append)
    runner.start(profile, request(tmp_path))
    try:
        wait(lambda: bool(results), 15000)
        assert results == ['done'] and runner.session_id == 'session-1'
        assert ''.join(e.text for e in events if e.kind == 'text').strip() == '사진 확인'
        assert [(e.text, e.count) for e in events if e.kind == 'tool'] == [('apply_edits', 2)]
    finally:
        runner.shutdown()


@pytest.mark.parametrize('mode,expected', [('stdin', 'done'), ('malformed', 'done'), ('fail', 'failed')])
def test_eof_malformed_and_failure(app, tmp_path, mode, expected, monkeypatch):
    monkeypatch.setenv('GEMINI_API_KEY', 'secret-key-in-stderr')
    peer = Path(__file__).with_name('fake_ai.py')
    profile = replace(ai.PROFILES[1], build=lambda r: [sys.executable, str(peer), 'codex', mode])
    runner = ai.AiRunner(); events = []; results = []
    runner.event.connect(events.append); runner.finished.connect(results.append)
    runner.start(profile, request(tmp_path))
    try:
        wait(lambda: bool(results), 15000)
        assert results == [expected]
        assert 'secret-key-in-stderr' not in repr(events)
        assert 'secret-key-in-stderr' not in runner.diagnostic
    finally:
        runner.shutdown()


def alive(pid):
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes as w
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]; api.OpenProcess.restype = w.HANDLE
        api.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
        api.CloseHandle.argtypes = [w.HANDLE]
        handle = api.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = w.DWORD(); api.GetExitCodeProcess(handle, ctypes.byref(code)); return code.value == 259
        finally:
            api.CloseHandle(handle)
    try:
        os.kill(pid, 0); return True
    except ProcessLookupError:
        return False


@pytest.mark.parametrize('action', ['stop', 'shutdown', 'timeout'])
def test_whole_process_tree_is_ended(app, tmp_path, action):
    pidfile = tmp_path / 'child.pid'
    peer = Path(__file__).with_name('fake_ai.py')
    profile = replace(ai.PROFILES[1], build=lambda r: [sys.executable, str(peer), 'codex', 'tree', str(pidfile)])
    runner = ai.AiRunner(); results = []; runner.finished.connect(results.append)
    runner.start(profile, request(tmp_path), timeout=2 if action == 'timeout' else 120)
    try:
        wait(pidfile.is_file, 15000); child = int(pidfile.read_text()); assert alive(child)
        if action != 'timeout':
            getattr(runner, action)()
        wait(lambda: bool(results), 15000)
        assert results == ['timeout' if action == 'timeout' else 'stopped']
        assert not alive(child)
    finally:
        runner.shutdown()


def test_immediate_stop_before_spawn(app, tmp_path):
    peer = Path(__file__).with_name('fake_ai.py')
    profile = replace(ai.PROFILES[1], build=lambda r: [sys.executable, str(peer), 'codex', 'tree', str(tmp_path / 'pid')])
    runner = ai.AiRunner(); results = []; runner.finished.connect(results.append)
    runner.start(profile, request(tmp_path)); runner.shutdown()
    wait(lambda: bool(results), 15000)
    assert results == ['stopped'] and not runner.running


def test_redaction_and_foreign_tools():
    assert ai.redact('Bearer abc sk-secret ' + 'a' * 64 + ' private', ('private',)) == '[redacted] [redacted] [redacted] [redacted]'
    assert ai.tool_event('Bash').kind == 'error'
    parser = ai.EventParser()
    assert parser.codex({'type': 'item.started', 'item': {'type': 'mcp_tool_call', 'server': 'other'}})[0].kind == 'error'
