# Ask AI validation — 2026-10-25

This change replaces the old Codex command window and isolated ChatGPT login. The
AI button is always visible. The shared photo workflow and MCP batch/undo code
remain in use. No version or release notes were changed. Nothing was deployed or
pushed, and the owner's old login folder was not removed.

## Real installed programs

Each run used `tools/try_ai.py`, three generated PNGs, a temporary library and
settings directory, and a free MCP port. None used the real library/settings or
the repository's `data/`. Reports contain status, tool names and catalog values,
not conversations or credentials.

| Program | Installed version | Real result | Stop |
| --- | --- | --- | --- |
| Claude Code | 2.1.186 | No photo edits: the service returned HTTP 401, "OAuth access token is invalid", although `claude auth status` reported a signed-in account. | The separate run stopped in 0.75 s. |
| Codex | 0.163.0 | No photo edits: no tool event within 90 s; a second 30 s startup check also timed out with no stderr. `codex login status` reported ChatGPT. A local mock API startup check also timed out before any request. Removing inherited parent-session variables and explicitly selecting gpt-6-astra still timed out after 60 s; no environment workaround was shipped. A final run with the static restricted model catalog also timed out at 60 s without tools or edits. | The separate run stopped in 0.55 s. |
| Gemini CLI | 0.29.4 | No photo edits: no tool event within 60 s. Plain `--help` also timed out at 20 s and 55 s, before this feature's wrapper/configuration was involved. Flags and tool exclusions were checked against the installed package's code. | The separate run stopped in 0.55 s. |

These are failed end-to-end edit checks, not successful edits. Real follow-up
edits were not attempted after the first edit failed; stand-ins cover follow-up
state and parsing. No account was reconfigured, refreshed by copying credentials,
or logged in from Grainy to get around these failures. Local reports:
`validation/ai-claude.json`, `validation/ai-codex.json`,
`validation/ai-codex-diagnostic.json`, `validation/ai-codex-clean-session.json`,
`validation/ai-gemini.json`, `validation/ai-codex-static-catalog.json`.

## Commands and storage decision

The [program profile table in DEVELOPMENT.md](../DEVELOPMENT.md#ai에게-시키기-ask-ai)
records the launch arguments and per-run settings. The full Codex feature switch
list is `CODEX_DISABLED` in `luma/ai_programs.py`; every entry is passed as
`-c features.<name>=false`. The following switches were used:

```text
shell_tool unified_exec shell_snapshot apply_patch_freeform
apps connectors plugins remote_plugin hooks codex_hooks plugin_hooks
multi_agent collab multi_agent_v2 js_repl browser_use computer_use
image_generation imagegenext view_image memory_tool memories skill_search
request_permissions request_permissions_tool goals tool_search code_mode
code_mode_only sleep_tool stable_environment_tools current_time_reminder
token_budget deferred_executor send_message_to_user_async tool_suggest
recommended_plugins skill_mcp_dependency_install
```

Claude's `--tools ""` removes built-ins, strict MCP configuration contains only
Grainy, and `dontAsk` permits only the allowed Grainy tools without prompting.
Codex disables shell/web/integrations and keeps a read-only sandbox with
`approval_policy="never"`; the Grainy server uses
`default_tools_approval_mode="approve"`. A final source audit found that Codex
0.163 advertises `apply_patch` from model metadata despite the old feature flag.
The launcher therefore passes a static per-run `model_catalog_json`, copied from
model cache entries without account identity: `apply_patch_tool_type=null`,
`shell_type="disabled"`, `experimental_supported_tools=[]`, and no model tool-mode
or agent override. The static manager does not refresh or merge remote catalogs.
Unknown explicitly requested models use minimal metadata without patch tools.
Agent and input-prompt configuration is disabled as well. The catalog boundary
and unchanged normal cache are covered by a dedicated test. Gemini's per-process settings have
`tools.core=[]`, trusted Grainy as the only allowed MCP server, and disabled
hooks, agents and extensions. Empty discovery/call/server command settings also
block inherited custom command tools. Diagnostics redact secret-valued environment
variables as well as the bridge token. No dangerous sandbox/approval bypass is used.
These restrictions were checked against installed/versioned source and stand-ins;
the failed live runs did not provide a model-request tool-schema capture.

Native `claude --resume <id>` and `codex exec resume <id>` exist in these versions,
but require their normal session files outside Grainy's settings folder. To honor
the brief's storage boundary, these profiles disable persistence and pass recent
conversation text from window memory to the next invocation. This is explicitly
shown in the UI. It does not preserve raw tool/image context. Gemini supports
`--resume <id>` with its project/session storage redirected in the process to
Grainy's own run folder; its normal authentication path is retained. New
Conversation and Grainy shutdown clean up this window's generated run folders.

`agy` is excluded because no per-run MCP configuration was established; its
`mcp add` command would modify user settings. Grok Build and Meta Muse Code were
not installed or tested and have no shipped profiles.

## UI and local checks

Korean and English offscreen renders were inspected:
`validation/ai-window/ko-notice.png` and
`validation/ai-window/en-conversation.png`. The first-use notice, disabled MCP
state, model/program controls, streaming tool line and undo controls fit without
clipping. Unit tests use only stand-in programs and temporary settings/catalogs.

Focused checks passed: launcher 19, AI dialog 8, native detection 2 (one macOS
case skipped on Windows), photo service 111, feedback 11 and MCP service 33.
An early batch-only run passed 25 functional checks but failed the unchanged
500-photo timing bound: apply + undo took 16.79 s against a 15 s limit. The full
check runs timing tests after all parallel workers have stopped. The first full
check passed 1,805 tests with 10 skipped, including the unchanged timing test.
The second full check passed 1,806 tests with 10 skipped in 315.19 s, including
all 47 interaction/batch checks. After the last custom-command blocking and
secret-redaction changes, the launcher file passed all 19 tests again.
The source self-test report has `status="passed"`, `ai_window=true`,
`photo_atomic_batch_undo=true`, and `mcp_protocol=true`. It used temporary data
and transmitted no photos. Reports: `validation/check-ai-window/report.json`,
`validation/check-ai-window-final/report.json`, and
`validation/source-self-test-ai-window.json`.

## English changelog summary

- Replace the legacy Codex login and command dialogs with Ask AI.
- Add an always-visible AI button and a non-modal streaming conversation window.
- Add per-run Claude Code, Codex and Gemini CLI profiles using Grainy's MCP bridge.
- Support follow-ups, model selection, Stop, New Conversation and AI batch undo.
- Contain child processes and keep Grainy-managed conversation data private.
- Add first-use privacy guidance, translations, offline tests and isolated live checks.
