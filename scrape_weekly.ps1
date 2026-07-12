# Weekly Boot Barn scrape -> shared Postgres. Run by Windows Task Scheduler.
# Loads .env, then runs prices + stores against DATABASE_URL. Logs to data\scrape.log.
$ErrorActionPreference = "Stop"
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
"$(Get-Date -Format o)  starting weekly scrape" | Out-File -Append -Encoding utf8 $log
& $py run.py all *>> $log
"$(Get-Date -Format o)  finished (exit $LASTEXITCODE)" | Out-File -Append -Encoding utf8 $log
