# Weekly Boot Barn job -> shared Postgres. Run by Windows Task Scheduler.
# Loads .env, scrapes prices + stores, then (if Dewey creds are set) syncs the
# latest foot-traffic weeks. Logs everything to data\scrape.log.
# ErrorActionPreference=Continue so a hiccup in one step never skips the others.
$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot

# Load .env into this process's environment.
if (Test-Path (Join-Path $PSScriptRoot ".env")) {
    Get-Content (Join-Path $PSScriptRoot ".env") |
        Where-Object { $_ -match '^\s*[^#].*=' } |
        ForEach-Object {
            $parts = $_ -split '=', 2
            [Environment]::SetEnvironmentVariable($parts[0].Trim(), $parts[1].Trim())
        }
}

$py  = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$log = Join-Path $PSScriptRoot "data\scrape.log"

"$(Get-Date -Format o)  === weekly run starting ===" | Out-File -Append -Encoding utf8 $log

# 1) Pricing + store roster
& $py run.py all *>> $log
"$(Get-Date -Format o)  scrape finished (exit $LASTEXITCODE)" | Out-File -Append -Encoding utf8 $log

# 2) Foot-traffic sync -- best effort, only if Dewey credentials are configured.
if ($env:DEWEY_API_KEY -and $env:DEWEY_PRODUCT_PATH) {
    "$(Get-Date -Format o)  starting foot-traffic sync" | Out-File -Append -Encoding utf8 $log
    & $py -m bbxray.ingest_dewey sync *>> $log
    "$(Get-Date -Format o)  foot-traffic sync finished (exit $LASTEXITCODE)" | Out-File -Append -Encoding utf8 $log
} else {
    "$(Get-Date -Format o)  skipping foot-traffic sync (Dewey creds not in .env)" | Out-File -Append -Encoding utf8 $log
}

# 3) Weekly news digest email (best-effort; needs DIGEST_TO + Gmail connected)
if ($env:DIGEST_TO) {
    "$(Get-Date -Format o)  building news digest" | Out-File -Append -Encoding utf8 $log
    & $py weekly_digest.py *>> $log
    "$(Get-Date -Format o)  news digest finished (exit $LASTEXITCODE)" | Out-File -Append -Encoding utf8 $log
}

"$(Get-Date -Format o)  === weekly run complete ===" | Out-File -Append -Encoding utf8 $log
