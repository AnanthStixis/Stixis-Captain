<#
.SYNOPSIS
  Captain desktop agent v1 (ADR-006): report AI desktop apps and MCP bypasses. Observe-only.

.DESCRIPTION
  Runs as the signed-in user (Intune / scheduled task, once a day). Sends ONE report to
  Captain's gateway (POST /v1/desktop/report):
    - which of ChatGPT, Claude, Cursor and VS Code are installed or running, with version;
    - which MCP servers Claude Desktop, Cursor and VS Code are configured to use: only each
      server's NAME, whether it is a local program (stdio) or remote (http), and the remote
      HOST name.
  It never reads or sends prompts, files, file paths, command arguments, environment
  variables, headers, tokens or the machine name. The device is a SHA-256 of the Windows
  machine id. It never blocks anything and changes nothing on the machine.

  Use -DryRun to print exactly what would be sent, without sending it.

.PARAMETER CaptainUrl
  The gateway, e.g. https://captain.corp.example. Plain http is refused except for this machine.

.PARAMETER KeyFile
  File holding the fleet's Captain key (a key whose principal has the desktop-agent role and
  whose system allows no models). Alternatively set CAPTAIN_DESKTOP_KEY in the environment.

.EXAMPLE
  .\Invoke-CaptainDesktopAgent.ps1 -CaptainUrl https://captain.corp.example -DryRun
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$CaptainUrl,
    [string]$KeyFile,
    [switch]$DryRun
)
$ErrorActionPreference = "Stop"
$AgentVersion = "1.0.0"

function Get-DeviceId {
    $guid = (Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Cryptography" -Name MachineGuid).MachineGuid
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $hash = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes("captain-device|$guid"))
    return (($hash | ForEach-Object { $_.ToString("x2") }) -join "").Substring(0, 32)
}

function Get-UserId {
    try {
        $upn = (& whoami.exe /upn 2>$null)
        if ($LASTEXITCODE -eq 0 -and $upn -match "@") { return $upn.Trim() }
    } catch { }
    return "$env:USERDOMAIN\$env:USERNAME"
}

function Get-CleanVersion([string]$v) {
    if (-not $v) { return $null }
    $clean = ($v -replace "[^0-9A-Za-z.\-+]", "")
    if ($clean.Length -gt 40) { $clean = $clean.Substring(0, 40) }
    if ($clean) { return $clean } else { return $null }
}

function Get-ExeVersion([string[]]$paths) {
    foreach ($p in $paths) {
        if ($p -and (Test-Path -LiteralPath $p)) {
            return @{ found = $true; version = (Get-CleanVersion (Get-Item -LiteralPath $p).VersionInfo.ProductVersion) }
        }
    }
    return @{ found = $false; version = $null }
}

function Get-AppxVersion([string]$namePattern) {
    try {
        $pkg = Get-AppxPackage -Name $namePattern -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($pkg) { return @{ found = $true; version = (Get-CleanVersion ([string]$pkg.Version)) } }
    } catch { }
    return @{ found = $false; version = $null }
}

# App -> how to find it. Install locations are the vendors' defaults (per-user installers
# and the Microsoft Store); they are not verified for every version.
$Apps = @(
    @{ app = "chatgpt"; processes = @("ChatGPT"); appx = "OpenAI.ChatGPT*";
       exes = @("$env:LOCALAPPDATA\Programs\ChatGPT\ChatGPT.exe") },
    @{ app = "claude"; processes = @("claude"); appx = "*Claude*";
       exes = @("$env:LOCALAPPDATA\AnthropicClaude\claude.exe", "$env:LOCALAPPDATA\Programs\claude\Claude.exe") },
    @{ app = "cursor"; processes = @("Cursor"); appx = $null;
       exes = @("$env:LOCALAPPDATA\Programs\cursor\Cursor.exe") },
    @{ app = "vscode"; processes = @("Code"); appx = $null;
       exes = @("$env:LOCALAPPDATA\Programs\Microsoft VS Code\Code.exe", "$env:ProgramFiles\Microsoft VS Code\Code.exe") }
)

function Get-AppsSeen {
    $out = @()
    foreach ($a in $Apps) {
        $exe = Get-ExeVersion $a.exes
        $store = if ($a.appx) { Get-AppxVersion $a.appx } else { @{ found = $false; version = $null } }
        $procs = @(Get-Process -Name $a.processes -ErrorAction SilentlyContinue)
        $running = $procs.Count -gt 0
        $version = if ($exe.version) { $exe.version } else { $store.version }
        $fromProcess = $false
        if ($running -and -not $version) {
            # Installed somewhere we did not look: the running process proves it. Only its
            # version is read; its path is never sent.
            $p = $procs | Where-Object { $_.Path } | Select-Object -First 1
            if ($p) { $version = Get-CleanVersion (Get-Item -LiteralPath $p.Path).VersionInfo.ProductVersion; $fromProcess = $true }
        }
        $out += [ordered]@{
            app       = $a.app
            installed = [bool]($exe.found -or $store.found -or $fromProcess)
            running   = $running
            version   = $version
        }
    }
    return $out
}

# MCP configuration files, per app. Only names, transport and remote host are read out.
$McpFiles = @(
    @{ app = "claude"; path = "$env:APPDATA\Claude\claude_desktop_config.json" },
    @{ app = "cursor"; path = "$env:USERPROFILE\.cursor\mcp.json" },
    @{ app = "vscode"; path = "$env:APPDATA\Code\User\mcp.json" }
)
Get-ChildItem -Path "$env:APPDATA\Code\User\profiles" -Directory -ErrorAction SilentlyContinue |
    ForEach-Object { $McpFiles += @{ app = "vscode"; path = (Join-Path $_.FullName "mcp.json") } }

function Get-McpServersSeen {
    $out = @()
    foreach ($f in $McpFiles) {
        if (-not (Test-Path -LiteralPath $f.path)) { continue }
        try { $doc = Get-Content -LiteralPath $f.path -Raw -Encoding UTF8 | ConvertFrom-Json } catch { continue }
        $section = $null
        foreach ($key in @("mcpServers", "servers")) {
            if ($doc.PSObject.Properties.Name -contains $key) { $section = $doc.$key; break }
        }
        if (-not $section) { continue }
        foreach ($prop in $section.PSObject.Properties) {
            $cfg = $prop.Value
            $name = [string]$prop.Name
            if ($name.Length -gt 80) { $name = $name.Substring(0, 80) }
            $url = $null
            if ($cfg.PSObject.Properties.Name -contains "url") { $url = [string]$cfg.url }
            if ($url) {
                $remoteHost = $null
                try { $remoteHost = ([Uri]$url).Host.ToLowerInvariant() } catch { $remoteHost = "unparseable" }
                if (-not $remoteHost) { $remoteHost = "unparseable" }
                $out += [ordered]@{ app = $f.app; server = $name; transport = "http"; remote_host = $remoteHost }
            } elseif ($cfg.PSObject.Properties.Name -contains "command") {
                # A local program: never governable by a proxy. Its command line is NOT read.
                $out += [ordered]@{ app = $f.app; server = $name; transport = "stdio"; remote_host = $null }
            }
        }
    }
    return $out
}

$report = [ordered]@{
    agent_version = $AgentVersion
    device        = Get-DeviceId
    user          = Get-UserId
    day           = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd")
    os            = "windows"
    apps          = @(Get-AppsSeen)
    mcp_servers   = @(Get-McpServersSeen)
}
$json = $report | ConvertTo-Json -Depth 5

if ($DryRun) {
    Write-Output "DRY RUN: this is exactly what would be sent to $CaptainUrl/v1/desktop/report"
    Write-Output $json
    exit 0
}

$uri = [Uri]("$($CaptainUrl.TrimEnd('/'))/v1/desktop/report")
if ($uri.Scheme -ne "https" -and $uri.Host -notin @("127.0.0.1", "localhost", "::1")) {
    throw "Refusing to send the key over plain http to $($uri.Host). Use https."
}
$key = $env:CAPTAIN_DESKTOP_KEY
if (-not $key -and $KeyFile) { $key = (Get-Content -LiteralPath $KeyFile -Raw).Trim() }
if (-not $key) { throw "No key: pass -KeyFile or set CAPTAIN_DESKTOP_KEY." }

$resp = Invoke-RestMethod -Method Post -Uri $uri -ContentType "application/json; charset=utf-8" `
    -Headers @{ Authorization = "Bearer $key" } -Body ([Text.Encoding]::UTF8.GetBytes($json)) -TimeoutSec 30
Write-Output ("recorded {0}: {1} app(s), {2} MCP bypass signal(s), {3} via Captain" -f `
    $resp.recorded, $resp.apps, $resp.bypass_signals, $resp.via_captain)
