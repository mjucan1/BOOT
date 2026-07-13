# Send due scheduled emails. Run by Task Scheduler every ~20 min.
# Loads .env (DATABASE_URL) and runs send_scheduled.py; the Gmail token comes
# from Supabase. Logs to data\scheduled_send.log.
$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot

if (Test-Path (Join-Path $PSScriptRoot ".env")) {
    Get-Content (Join-Path $PSScriptRoot ".env") |
        Where-Object { $_ -match '^\s*[^#].*=' } |
        ForEach-Object {
            $parts = $_ -split '=', 2
            [Environment]::SetEnvironmentVariable($parts[0].Trim(), $parts[1].Trim())
        }
}

$py  = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$log = Join-Path $PSScriptRoot "data\scheduled_send.log"
& $py send_scheduled.py *>> $log
