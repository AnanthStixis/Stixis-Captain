<#
.SYNOPSIS
  Install (or remove) the Captain desktop agent as a scheduled task for the signed-in user.

.DESCRIPTION
  Copies the agent to %LOCALAPPDATA%\Stixis\CaptainAgent and registers a task that runs it
  at logon and every day at 12:00, as the user (MCP configs and app installs are per user).
  Intune: deploy as a user-context PowerShell script, or wrap in a Win32 app with this as
  the install command and `-Uninstall` as the uninstall command.

.EXAMPLE
  .\Register-CaptainDesktopAgent.ps1 -CaptainUrl https://captain.corp.example -KeyFile \\share\captain\desktop-fleet.key
.EXAMPLE
  .\Register-CaptainDesktopAgent.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [string]$CaptainUrl,
    [string]$KeyFile,
    [switch]$Uninstall
)
$ErrorActionPreference = "Stop"
$TaskName = "Stixis Captain desktop agent"
$Dir = Join-Path $env:LOCALAPPDATA "Stixis\CaptainAgent"

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    if (Test-Path $Dir) { Remove-Item -Recurse -Force $Dir }
    Write-Output "removed"
    exit 0
}
if (-not $CaptainUrl -or -not $KeyFile) { throw "Pass -CaptainUrl and -KeyFile (or -Uninstall)." }

New-Item -ItemType Directory -Force $Dir | Out-Null
Copy-Item (Join-Path $PSScriptRoot "Invoke-CaptainDesktopAgent.ps1") $Dir -Force
# The fleet key is stored for this user only (it can only send desktop reports).
$keyPath = Join-Path $Dir "fleet.key"
Copy-Item -LiteralPath $KeyFile $keyPath -Force
icacls $keyPath /inheritance:r /grant:r "$($env:USERNAME):(R)" | Out-Null

$agent = Join-Path $Dir "Invoke-CaptainDesktopAgent.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$agent`" -CaptainUrl `"$CaptainUrl`" -KeyFile `"$keyPath`""
$triggers = @(
    (New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"),
    (New-ScheduledTaskTrigger -Daily -At "12:00")
)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers -Settings $settings `
    -Description "Reports AI desktop apps and MCP bypasses to Stixis Captain (metadata only)." -Force | Out-Null
Write-Output "installed: '$TaskName' runs at logon and daily at 12:00"
