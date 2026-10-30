param([string]$Url = "http://127.0.0.1:8080")
# Exit codes via stdout: 1 = memory on, 0 = memory off/dry-run, 2 = cannot tell.
$ErrorActionPreference = "SilentlyContinue"
$tokFile = Join-Path $env:USERPROFILE ".deepseek\bridge_token"
if (-not (Test-Path $tokFile)) { "2"; exit }
$tok = (Get-Content $tokFile -Raw).Trim()
try {
  $r = Invoke-WebRequest -Uri "$Url/api/state" -Headers @{ "X-Auth" = $tok } -UseBasicParsing -TimeoutSec 5
  $j = $r.Content | ConvertFrom-Json
  if ($j.memory.enabled -and -not $j.memory.dry_run) { "1" } else { "0" }
} catch {
  "2"
}
