#!/bin/sh
# Grainy MCP bridge on macOS: how Claude Code, Codex and Claude Desktop start Grainy's MCP (stdio).
# One JSON-RPC message per stdin line; one reply line per request on stdout.
# Grainy writes this file and the files it reads (endpoint, auth-header, offline-*.json) into its own
# settings folder, ~/Library/Application Support/Grainy/mcp; the endpoint and the key are read again for
# every request.
DIR=${GRAINY_MCP_DIR:-$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)}
CURL=${GRAINY_MCP_CURL:-/usr/bin/curl}

field() {   # field NAME LINE: the top-level JSON value of NAME, as written (number or "string"), or nothing
  printf '%s\n' "$2" | awk -v want="$1" '
  { s = $0; n = length(s); depth = 0; state = ""; key = ""; i = 1
    while (i <= n) {
      c = substr(s, i, 1)
      if (c == "\"") {
        j = i + 1
        while (j <= n) { d = substr(s, j, 1); if (d == "\\") { j += 2; continue }; if (d == "\"") break; j++ }
        tok = substr(s, i, j - i + 1)
        if (depth == 1 && state == "key") { key = substr(tok, 2, length(tok) - 2); state = "colon" }
        else if (depth == 1 && state == "value") { if (key == want) { print tok; exit } ; state = "" }
        i = j + 1; continue
      }
      if (c == "{" || c == "[") { depth++; if (depth == 1) state = "key"; else if (depth == 2 && state == "value") state = ""; i++; continue }
      if (c == "}" || c == "]") { depth--; i++; continue }
      if (depth == 1) {
        if (c == ":" && state == "colon") { state = "value"; i++; continue }
        if (c == ",") { state = "key"; i++; continue }
        if (state == "value" && c !~ /[ \t]/) {
          j = i; while (j <= n && substr(s, j, 1) !~ /[,} \t]/) j++
          if (key == want) { print substr(s, i, j - i); exit }
          state = ""; i = j; continue
        }
      }
      i++
    } }'
}
reply() { printf '{"jsonrpc":"2.0","id":%s,%s}\n' "$1" "$2"; }
offline() {
  id=$(field id "$1"); method=$(field method "$1")
  [ -n "$id" ] && [ -n "$method" ] || return 0
  case "$method" in
    '"ping"') reply "$id" '"result":{}' ;;
    '"resources/list"') reply "$id" '"result":{"resources":[]}' ;;
    '"resources/templates/list"') reply "$id" '"result":{"resourceTemplates":[]}' ;;
    '"prompts/list"') reply "$id" '"result":{"prompts":[]}' ;;
    '"initialize"'|'"tools/list"'|'"tools/call"')
      case "$method" in '"initialize"') f=offline-initialize.json ;; '"tools/list"') f=offline-tools.json ;; *) f=offline-call.json ;; esac
      if [ -f "$DIR/$f" ]; then
        esc=$(printf '%s' "$id" | sed 's/[\\&|]/\\&/g')
        sed "s|__ID__|$esc|" "$DIR/$f" | tr -d '\r\n'; printf '\n'
      else reply "$id" '"error":{"code":-32603,"message":"Grainy is not running, or its AI connection is off. Start Grainy, or turn its AI connection on (Grainy: Settings > AI Connection (MCP)\u2026). / Grainy\uac00 \uc2e4\ud589 \uc911\uc774 \uc544\ub2c8\uac70\ub098 AI \uc5f0\uacb0\uc774 \uaebc\uc838 \uc788\uc2b5\ub2c8\ub2e4. Grainy\ub97c \uc2e4\ud589\ud558\uac70\ub098 Grainy\uc758 \uc124\uc815 \u203a AI \uc5f0\uacb0 (MCP)\u2026\uc5d0\uc11c AI \uc5f0\uacb0\uc744 \ucf1c\uc138\uc694."}'; fi ;;
    *) reply "$id" '"error":{"code":-32601,"message":"Method not found"}' ;;
  esac
}
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in *[![:space:]]*) ;; *) continue ;; esac
  if [ -f "$DIR/endpoint" ] && [ -f "$DIR/auth-header" ] && \
     body=$(printf '%s' "$line" | "$CURL" -sS --fail --noproxy '*' --max-time 56 -X POST \
       -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -H 'Expect:' \
       -H @"$DIR/auth-header" --data-binary @- "$(cat "$DIR/endpoint")" 2>/dev/null); then
    [ -n "$body" ] && { printf '%s' "$body" | tr '\r\n' '  '; printf '\n'; }
    continue
  fi
  offline "$line"
done
exit 0
