# Grainy MCP bridge on Windows: how Claude Code, Codex and Claude Desktop start Grainy's MCP (stdio).
# One JSON-RPC message per stdin line; one reply line per request on stdout. Logs go to stderr.
# Grainy writes this file and the files it reads (endpoint, auth-header, offline-*.json) into its own
# settings folder, %LOCALAPPDATA%\Grainy\mcp; the endpoint and the key are read again for every request.
$ErrorActionPreference = 'Stop'
try {
  $dir = if ($env:GRAINY_MCP_DIR) { $env:GRAINY_MCP_DIR } elseif ($PSScriptRoot) { $PSScriptRoot } else { Join-Path $env:LOCALAPPDATA 'Grainy\mcp' }
  $utf8 = New-Object System.Text.UTF8Encoding($false)
  Add-Type -AssemblyName System.Net.Http
  $handler = New-Object System.Net.Http.HttpClientHandler; $handler.UseProxy = $false
  $client = New-Object System.Net.Http.HttpClient($handler); $client.Timeout = [TimeSpan]::FromSeconds(56)
  $client.DefaultRequestHeaders.ExpectContinue = $false
  $stdin = New-Object System.IO.StreamReader([Console]::OpenStandardInput(), $utf8)
  $stdout = [Console]::OpenStandardOutput()
  function Read-Text($name) { $p = Join-Path $dir $name; if (Test-Path -LiteralPath $p) { [IO.File]::ReadAllText($p, $utf8).Trim() } else { $null } }
  function Send-Line($text) { $b = $utf8.GetBytes(($text -replace "[`r`n]+", ' ') + "`n"); $stdout.Write($b, 0, $b.Length); $stdout.Flush() }
  # Send once: the reply text, or 'retry' when nothing reached Grainy (no connection files, nothing listening on the
  # port, or the key being replaced), or $null when Grainy may have got the request (no answer in time).
  function Send($line) {
    $url = Read-Text 'endpoint'; $auth = Read-Text 'auth-header'
    if (-not $url -or -not $auth) { return 'retry' }
    try {
      $req = New-Object System.Net.Http.HttpRequestMessage([System.Net.Http.HttpMethod]::Post, $url)
      $req.Content = New-Object System.Net.Http.StringContent($line, $utf8, 'application/json')
      [void]$req.Headers.TryAddWithoutValidation('Accept', 'application/json, text/event-stream')
      [void]$req.Headers.TryAddWithoutValidation('Authorization', $auth.Substring($auth.IndexOf(':') + 1).Trim())
      $res = $client.SendAsync($req).GetAwaiter().GetResult()
      if ($res.IsSuccessStatusCode) { return ,($res.Content.ReadAsStringAsync().GetAwaiter().GetResult()) }
      if ([int]$res.StatusCode -eq 401) { return 'retry' }
      return $null
    } catch {
      $e = $_.Exception
      while ($e) {
        if (($e -is [System.Net.WebException] -and $e.Status -eq 'ConnectFailure') -or
            ($e -is [System.Net.Sockets.SocketException] -and $e.SocketErrorCode -eq 'ConnectionRefused')) { return 'retry' }
        $e = $e.InnerException
      }
      return $null
    }
  }
  # A tool call made while Grainy is starting (its connection files appear once its window is up) or restarting
  # its connection waits for it up to 4 s; nothing was sent before, so it cannot run twice. Other requests answer
  # at once (offline when Grainy is not there).
  function Forward($line) {
    $body = Send $line
    if ($body -ne 'retry') { return ,$body }
    if ($line -notmatch '"method"\s*:\s*"tools/call"') { return $null }
    $until = [DateTime]::UtcNow.AddSeconds(4)
    while ([DateTime]::UtcNow -lt $until) {
      Start-Sleep -Milliseconds 250
      $body = Send $line
      if ($body -ne 'retry') { return ,$body }
    }
    return $null
  }
  function Offline($line) {
    try { $m = $line | ConvertFrom-Json } catch { return $null }
    if (-not ($m.PSObject.Properties.Name -contains 'id') -or $null -eq $m.method) { return $null }
    $id = ConvertTo-Json -Compress -InputObject $m.id
    $fixed = @{ 'ping' = '{}'; 'resources/list' = '{"resources":[]}'; 'resources/templates/list' = '{"resourceTemplates":[]}'; 'prompts/list' = '{"prompts":[]}' }
    if ($fixed.ContainsKey($m.method)) { return '{"jsonrpc":"2.0","id":' + $id + ',"result":' + $fixed[$m.method] + '}' }
    $file = @{ 'initialize' = 'offline-initialize.json'; 'tools/list' = 'offline-tools.json'; 'tools/call' = 'offline-call.json' }[$m.method]
    if (-not $file) { return '{"jsonrpc":"2.0","id":' + $id + ',"error":{"code":-32601,"message":"Method not found"}}' }
    $template = Read-Text $file
    if (-not $template) { return '{"jsonrpc":"2.0","id":' + $id + ',"error":{"code":-32603,"message":"Grainy is not running, or its AI connection is off. Start Grainy, or turn its AI connection on (Grainy: Settings > AI Connection (MCP)\u2026). / Grainy\uac00 \uc2e4\ud589 \uc911\uc774 \uc544\ub2c8\uac70\ub098 AI \uc5f0\uacb0\uc774 \uaebc\uc838 \uc788\uc2b5\ub2c8\ub2e4. Grainy\ub97c \uc2e4\ud589\ud558\uac70\ub098 Grainy\uc758 \uc124\uc815 \u203a AI \uc5f0\uacb0 (MCP)\u2026\uc5d0\uc11c AI \uc5f0\uacb0\uc744 \ucf1c\uc138\uc694."}}' }
    return $template.Replace('__ID__', $id)
  }
  while ($null -ne ($line = $stdin.ReadLine())) {
    if ($line.Trim().Length -eq 0) { continue }
    $body = Forward $line
    if ($null -ne $body) { if ($body.Trim().Length -gt 0) { Send-Line $body.Trim() }; continue }
    $reply = Offline $line
    if ($reply) { Send-Line $reply }
  }
  exit 0
} catch { [Console]::Error.WriteLine('grainy-mcp-bridge: ' + $_.Exception.Message); exit 1 }
