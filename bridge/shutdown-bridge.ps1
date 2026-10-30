param([string]$Url = "http://127.0.0.1:8080")
# Ask the bridge to shut down, then wait for port 8080 to be released.
$ErrorActionPreference = "SilentlyContinue"
$tokFile = Join-Path $env:USERPROFILE ".deepseek\bridge_token"
$tok = if (Test-Path $tokFile) { (Get-Content $tokFile -Raw).Trim() } else { "" }
try {
  Invoke-WebRequest -Method POST -Uri "$Url/shutdown" -Headers @{ "X-Auth" = $tok } -UseBasicParsing -TimeoutSec 8 | Out-Null
} catch {}
for ($i = 0; $i -lt 20; $i++) {
  if (-not (Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue)) { exit 0 }
  Start-Sleep -Seconds 1
}
exit 1
