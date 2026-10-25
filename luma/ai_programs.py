"""CLI profiles for Ask AI. Official account actions live in ai_auth.

The table owns discovery, arguments, event decoding and conversation resumption.
Only the Grainy settings directory is used for files written by this module.
"""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sys
import threading
from typing import Callable

from PySide6.QtCore import QObject, Signal

from .auth import find_codex
from .ai_process import ProcessTree
from .ai_auth import authentication_failed
from .ai_install import INSTALLERS
from .ai_paths import find_tool


def find_program(name):
    """Prefer executables; execute npm's JS with node, never interpolate a cmd file."""
    found = find_tool(name)
    if not found and name == 'claude':
        candidate = Path.home() / '.local' / 'bin' / ('claude.exe' if os.name == 'nt' else 'claude')
        if candidate.is_file():
            found = str(candidate)
    if not found:
        return None
    path = Path(found)
    if os.name != 'nt' and name == 'gemini':
        entry = path.resolve()
        node = find_tool('node')
        if node and entry.suffix == '.js':
            return (node, str(entry))
        return None
    if os.name == 'nt' and path.suffix.lower() in ('.cmd', '.ps1', '.bat', ''):
        packages = {'gemini': ('@google/gemini-cli', 'dist/index.js'),
                    'claude': ('@anthropic-ai/claude-code', 'cli.js')}
        package, entry = packages[name]
        script = path.parent / 'node_modules' / package / entry
        node = find_tool('node')
        return (node, str(script)) if node and script.is_file() else None
    return (str(path),)


def codex_program():
    path = find_codex()
    return (path,) if path else None


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    if os.name != 'nt':
        path.chmod(0o600)


@dataclass
class Request:
    executable: tuple
    workspace: Path
    server: dict
    prompt: str
    language: str = 'ko'
    model: str = ''
    session_id: str = ''

    @property
    def instruction(self):
        language = 'Korean' if self.language == 'ko' else 'English'
        return (f'You are working inside Grainy, a photo editor, for the person using it. '
                f'Use only the grainy MCP tools to inspect and change the photo library. '
                f'Answer in {language}. Check tool results before reporting success. '
                'Do not execute commands, edit files, browse the web, or use other services. '
                'For a job, use get_job until it finishes. Do not invent photo ids.')


def claude_command(r):
    config = r.workspace / 'claude-mcp.json'
    write_json(config, {'mcpServers': {'grainy': r.server}})
    args = [*r.executable, '-p', r.prompt, '--output-format', 'stream-json', '--verbose',
            '--include-partial-messages', '--mcp-config', str(config), '--strict-mcp-config',
            '--tools', '', '--allowedTools', 'mcp__grainy__*', '--permission-mode', 'dontAsk',
            '--append-system-prompt', r.instruction, '--disable-slash-commands', '--no-chrome',
            '--setting-sources', '', '--settings', '{"disableAllHooks":true,"autoMemoryEnabled":false}',
            '--no-session-persistence']
    if r.model:
        args += ['--model', r.model]
    return args


# Validated against the installed 0.163 CLI/schema. Ignoring user config also
# prevents unrelated MCPs, plugins and hooks from being imported into this run.
CODEX_DISABLED = ('shell_tool', 'unified_exec', 'shell_snapshot', 'apply_patch_freeform',
                  'apps', 'connectors', 'plugins', 'remote_plugin', 'hooks', 'codex_hooks',
                  'plugin_hooks', 'multi_agent', 'collab', 'multi_agent_v2', 'js_repl',
                  'browser_use', 'computer_use', 'image_generation', 'imagegenext',
                  'view_image', 'memory_tool', 'memories', 'skill_search', 'request_permissions',
                  'request_permissions_tool', 'goals', 'tool_search', 'code_mode',
                  'code_mode_only', 'sleep_tool', 'stable_environment_tools', 'current_time_reminder',
                  'token_budget', 'deferred_executor', 'send_message_to_user_async', 'tool_suggest',
                  'recommended_plugins', 'skill_mcp_dependency_install')



def codex_catalog(r):
    """Static per-run catalog removes tools that feature flags do not control.

    Codex 0.163 advertises apply_patch from model metadata even when the legacy
    apply_patch_freeform feature is false. A static catalog suppresses remote
    refresh/merging. Unknown explicitly selected models use Codex's minimal
    fallback, whose apply_patch_tool_type is None and experimental tools empty.
    Only read the normal cache; never modify it or copy account/cache identity.
    """
    cache = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex') / 'models_cache.json'
    try:
        cached = json.loads(cache.read_text(encoding='utf-8'))
        models = cached.get('models', []) if isinstance(cached, dict) else []
    except (OSError, ValueError):
        models = []
    if not isinstance(models, list) or not all(isinstance(m, dict) for m in models):
        models = []
    if not models and not r.model:
        raise ValueError('Codex needs its model cache or an explicit model')
    for model in models:
        model.update(apply_patch_tool_type=None, shell_type='disabled',
                     experimental_supported_tools=[], supports_search_tool=False,
                     include_skills_usage_instructions=False, include_plugin_usage_instructions=False,
                     include_apps_usage_instructions=False, tool_mode=None, multi_agent_version=None)
    path = r.workspace / 'codex-models.json'
    write_json(path, {'models': models})
    return path

def codex_command(r):
    args = [*r.executable, 'exec', '--json', '--skip-git-repo-check',
            '--ignore-user-config', '--ignore-rules', '--ephemeral', '-C', str(r.workspace), '-s', 'read-only']
    config = {'approval_policy': 'never', 'web_search': 'disabled',
              'model_catalog_json': str(codex_catalog(r)), 'agents.enabled': False,
              'tools.experimental_request_user_input.enabled': False,
              'mcp_servers.grainy.command': r.server['command'],
              'mcp_servers.grainy.args': r.server['args'],
              'mcp_servers.grainy.default_tools_approval_mode': 'approve',
              'mcp_servers.grainy.startup_timeout_sec': 30,
              'developer_instructions': r.instruction, 'project_doc_max_bytes': 0,
              'features.skip_host_skill_discovery': True, 'tools.update_plan.enabled': False,
              'history.persistence': 'none', 'log_dir': str(r.workspace / 'codex-log'),
              'sqlite_home': str(r.workspace / 'codex-state')}
    config.update({f'features.{name}': False for name in CODEX_DISABLED})
    for key, value in config.items():
        args += ['-c', f'{key}={json.dumps(value, ensure_ascii=False)}']
    if r.model:
        args += ['-m', r.model]
    args += [r.prompt]
    return args


def gemini_command(r):
    settings = {
        'mcpServers': {'grainy': {**r.server, 'trust': True}},
        'mcp': {'allowed': ['grainy'], 'serverCommand': ''}, 'tools': {'core': [], 'allowed': ['grainy'], 'discoveryCommand': '', 'callCommand': ''},
        'hooksConfig': {'enabled': False}, 'context': {'fileName': []},
        'general': {'enableAutoUpdate': False}, 'telemetry': {'enabled': False},
        'experimental': {'enableAgents': False},
        'agents': {'overrides': {'codebase_investigator': {'enabled': False}, 'cli_help': {'enabled': False}}},
        'security': {'folderTrust': {'enabled': False}}, 'admin': {'extensions': {'enabled': False}},
    }
    write_json(r.workspace / '.gemini' / 'settings.json', settings)
    # The normal auth/home is retained. The small Node entrypoint redirects only
    # Gemini's project/session storage; it never copies or opens credentials.
    # Gemini 0.29.4 has no CLI switch for its auto-recorded conversation directory.
    if len(r.executable) != 2 or not r.executable[1].endswith('.js'):
        raise ValueError('Gemini needs the supported npm installation')
    entry = Path(r.executable[1]).resolve()
    storage = entry.parent.parent / 'node_modules/@google/gemini-cli-core/dist/src/config/storage.js'
    if not storage.is_file():
        raise ValueError('Unsupported Gemini storage layout')
    wrapper = r.workspace / 'gemini-entry.mjs'
    state = str(r.workspace / 'gemini-state')
    wrapper.write_text(
        'import {Storage} from ' + json.dumps(storage.as_uri()) + ';\n'
        'Storage.getGlobalTempDir = () => ' + json.dumps(state) + ';\n'
        'Storage.prototype.initialize = async function() { this.projectIdentifier = "grainy"; };\n'
        'Storage.prototype.getHistoryDir = function() { return ' + json.dumps(state + '/history') + '; };\n'
        'process.argv[1] = ' + json.dumps(str(entry)) + ';\n'
        'await import(' + json.dumps(entry.as_uri()) + ');\n', encoding='utf-8')
    args = [r.executable[0], str(wrapper), '--allowed-mcp-server-names', 'grainy', '--approval-mode', 'default',
            '--extensions', 'none', '--output-format', 'stream-json', '-p', r.prompt + '\n\n' + r.instruction]
    if r.model:
        args += ['--model', r.model]
    if r.session_id:
        args += ['--resume', r.session_id]
    return args


@dataclass(frozen=True)
class Event:
    kind: str
    text: str = ''
    count: int | None = None


def tool_event(name, arguments=None):
    name = str(name).removeprefix('mcp__grainy__').removeprefix('grainy__')
    # Only names from the shipped tool manifest, never a whole argument/result.
    from .mcp_tools import DEFINITIONS
    if name not in {item['name'] for item in DEFINITIONS}:
        return Event('error', 'Unexpected tool outside Grainy')
    args = arguments if isinstance(arguments, dict) else {}
    count = len(args['ids']) if isinstance(args.get('ids'), list) else None
    if isinstance(args.get('edits'), list):
        groups = args['edits']
        if all(isinstance(g, dict) and isinstance(g.get('ids'), list) for g in groups):
            count = len({i for group in groups for i in group['ids'] if isinstance(i, int)})
    return Event('tool', name, count)


class EventParser:
    """State is per invocation, so streamed deltas and final messages do not repeat."""
    def __init__(self):
        self.seen = set()
        self.partial = False
        self.succeeded = False
        self.failed = False

    def error(self, data):
        self.failed = True
        return [Event('auth_error' if authentication_failed(data) else 'error')]

    def once(self, key, event):
        if key in self.seen:
            return []
        self.seen.add(key)
        return [event]

    def claude(self, data):
        kind = data.get('type')
        if kind == 'system' and data.get('session_id'):
            return [Event('session', data['session_id'])]
        if kind == 'error' or (kind == 'assistant' and data.get('error')):
            return self.error(data)
        if kind == 'stream_event' and data.get('event', {}).get('type') == 'error':
            return self.error(data['event'])
        if kind == 'stream_event':
            event = data.get('event', {})
            if event.get('type') == 'message_start':
                self.partial = False
            delta = event.get('delta', {})
            if delta.get('type') == 'text_delta':
                self.partial = True
                return [Event('text', delta.get('text', ''))]
        if kind == 'assistant':
            out = []
            for part in data.get('message', {}).get('content', []):
                if part.get('type') == 'text' and not self.partial:
                    out.append(Event('text', part.get('text', '')))
                elif part.get('type') == 'tool_use':
                    out += self.once(part.get('id'), tool_event(part.get('name'), part.get('input')))
            self.partial = False
            return out
        if kind == 'result':
            self.failed = self.failed or bool(data.get('is_error'))
            self.succeeded = not self.failed
            return self.error(data) if self.failed else []
        return []

    def codex(self, data):
        kind = data.get('type')
        if kind == 'thread.started':
            return [Event('session', data['thread_id'])]
        item = data.get('item', {})
        if kind == 'item.completed' and item.get('type') == 'agent_message':
            return self.once(item.get('id'), Event('text', item.get('text', '') + '\n'))
        if kind in ('item.started', 'item.completed') and item.get('type') == 'mcp_tool_call':
            if item.get('server') != 'grainy':
                return [Event('error', 'Unexpected MCP server outside Grainy')]
            return self.once(item.get('id'), tool_event(item.get('tool'), item.get('arguments')))
        if kind in ('error', 'turn.failed'):
            return self.error(data)
        if kind == 'turn.completed':
            self.succeeded = True
        return []

    def gemini(self, data):
        kind = data.get('type')
        if kind == 'init' and data.get('session_id'):
            return [Event('session', data['session_id'])]
        if kind == 'message' and data.get('role') == 'assistant':
            return [Event('text', data.get('content', ''))]
        if kind == 'tool_use':
            return self.once(data.get('tool_id'), tool_event(data.get('tool_name'), data.get('parameters')))
        if kind == 'error':
            return self.error(data)
        if kind == 'result':
            self.failed = self.failed or data.get('status') != 'success'
            self.succeeded = not self.failed
            if self.failed:
                return self.error(data)
        return []


@dataclass(frozen=True)
class Profile:
    key: str
    name: str
    detect: Callable
    build: Callable
    parse: Callable
    installers: dict
    resume: str
    persistent: bool = False
    status_args: tuple | None = None
    login_args: tuple = ()
    auth_exit_codes: tuple = ()

    def installer(self, platform=None):
        return self.installers.get(sys.platform if platform is None else platform)


PROFILES = (
    Profile('claude', 'Claude Code', lambda: find_program('claude'), claude_command, EventParser.claude,
            INSTALLERS['claude'], 'in-memory context (no session persistence)',
            status_args=('auth', 'status', '--json'), login_args=('auth', 'login')),
    Profile('codex', 'Codex', codex_program, codex_command, EventParser.codex,
            INSTALLERS['codex'], 'in-memory context (--ephemeral)',
            status_args=('login', 'status'), login_args=('login',)),
    Profile('gemini', 'Gemini CLI', lambda: find_program('gemini'), gemini_command, EventParser.gemini,
            INSTALLERS['gemini'], '--resume <session id>', True, auth_exit_codes=(41,)),
)


def redact(text, secrets=()):
    text = str(text)
    for secret in secrets:
        if secret:
            text = text.replace(secret, '[redacted]')
    text = re.sub(r'(?i)(Bearer\s+)\S+|\b(?:sk-|sk-ant-)[A-Za-z0-9_-]+|\b[0-9a-f]{64}\b', '[redacted]', text)
    return text


class AiRunner(QObject):
    event = Signal(object)
    finished = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.running = False
        self.session_id = ''
        self.thread = None
        self.tree = None
        self.cancel = threading.Event()
        self.lock = threading.Lock()
        self.diagnostic = ''

    def start(self, profile, request, *, secrets=(), timeout=600):
        if self.running:
            raise RuntimeError('AI is already running')
        request.workspace.mkdir(parents=True, exist_ok=True)
        argv = profile.build(request)
        self.diagnostic = ''
        self.cancel.clear()
        self.running = True
        self.thread = threading.Thread(target=self._run, args=(profile, request, argv, secrets, timeout), daemon=True)
        self.thread.start()

    def _run(self, profile, request, argv, secrets, timeout):
        parser = EventParser()
        tree = None
        result = 'failed'
        stderr = bytearray()
        auth_failed = threading.Event()
        timer = None
        try:
            env = dict(os.environ)
            secrets = (*secrets, *(value for key, value in env.items()
                                  if len(value) >= 8 and re.search(r'(API_KEY|TOKEN|SECRET|PASSWORD)$', key, re.I)))
            if profile.key == 'claude':
                env['CLAUDE_CODE_DEBUG_LOGS_DIR'] = str(request.workspace / 'claude-log')
            if profile.key == 'gemini':
                # System-scope per-process overlay wins even over a user tools.core list.
                env['GEMINI_CLI_SYSTEM_SETTINGS_PATH'] = str(request.workspace / '.gemini/settings.json')
            tree = ProcessTree(argv, request.workspace, env)
            with self.lock:
                self.tree = tree
                if self.cancel.is_set():
                    tree.stop()
            def errors():
                while chunk := tree.process.stderr.read1(4096):
                    # Bounded, in-memory only. Never print raw provider diagnostics.
                    stderr.extend(chunk)
                    del stderr[:-8192]
                    if authentication_failed(stderr.decode('utf-8', 'replace')):
                        auth_failed.set()
            reader = threading.Thread(target=errors, daemon=True)
            reader.start()
            timed_out = threading.Event()
            def expire():
                timed_out.set()
                tree.stop()
            timer = threading.Timer(timeout, expire)
            timer.daemon = True
            timer.start()
            while line := tree.process.stdout.readline(4 * 1024 * 1024):
                try:
                    data = json.loads(line)
                    if not isinstance(data, dict):
                        continue
                    events = profile.parse(parser, data)
                except (ValueError, TypeError, KeyError, AttributeError):
                    if authentication_failed(line.decode('utf-8', 'replace')):
                        auth_failed.set()
                    continue
                for event in events:
                    if event.kind == 'session':
                        self.session_id = event.text
                    if event.kind in ('error', 'auth_error'):
                        parser.failed = True
                        if event.kind == 'auth_error':
                            auth_failed.set()
                        continue  # Emit a single fixed failure after the process exits.
                    self.event.emit(Event(event.kind, redact(event.text, secrets), event.count))
            code = tree.process.wait()
            tree.stop()  # close any bridge/launcher that survived its parent
            reader.join(timeout=2)
            if self.cancel.is_set():
                result = 'stopped'
            elif timed_out.is_set():
                result = 'timeout'
            elif (code != 0 or parser.failed or not parser.succeeded) and (auth_failed.is_set() or code in profile.auth_exit_codes):
                result = 'auth_required'
                self.diagnostic = 'authentication required'
                self.event.emit(Event('auth_error'))
            elif code == 0 and parser.succeeded and not parser.failed:
                result = 'done'
            else:
                # Diagnostics can contain credentials or entire HTTP requests.
                # Neither parsed errors nor raw stderr are stored or displayed.
                self.diagnostic = 'program failed'
                self.event.emit(Event('exit', str(code)))
        except (OSError, RuntimeError):
            self.event.emit(Event('launch_error'))
        finally:
            if timer:
                timer.cancel()
            with self.lock:
                self.tree = None
            if tree:
                tree.close()
            self.running = False
            self.finished.emit(result)

    def stop(self):
        self.cancel.set()
        with self.lock:
            if self.tree:
                self.tree.stop()

    def shutdown(self):
        self.stop()
        if self.thread:
            self.thread.join(timeout=12)
