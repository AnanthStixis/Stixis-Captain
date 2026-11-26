# Collect the release packages the dashboard offers for download (plan step 4) into
# backend\artifacts\, in the layout CAPTAIN_ARTIFACTS_DIR expects. The Docker image copies
# this folder (see Dockerfile). Nothing here is company-specific: each download is preset
# with the install's own address when someone downloads it.
#   .\scripts\build_artifacts.ps1            from the backend folder
# Needs Node.js (VS Code extension and browser extension builds).
$ErrorActionPreference = "Stop"
$backend = Split-Path -Parent $PSScriptRoot
$repo = Split-Path -Parent $backend
$out = Join-Path $backend "artifacts"
if (Test-Path $out) { Remove-Item -Recurse -Force $out }
New-Item -ItemType Directory -Force $out | Out-Null

# VS Code extension (.vsix)
Push-Location (Join-Path $repo "ide\vscode")
npm ci --silent
npm run package --silent
Pop-Location
New-Item -ItemType Directory -Force (Join-Path $out "ide\vscode") | Out-Null
Copy-Item (Join-Path $repo "ide\vscode\captain-vscode-*.vsix") (Join-Path $out "ide\vscode")

# Browser extension: a neutral build; each download rewrites the address it may talk to.
Push-Location (Join-Path $repo "browser\extension")
node build.mjs --captain http://127.0.0.1:8000 --out dist
Pop-Location
New-Item -ItemType Directory -Force (Join-Path $out "browser\extension") | Out-Null
Copy-Item -Recurse (Join-Path $repo "browser\extension\dist") (Join-Path $out "browser\extension\dist")

# Desktop agent and Claude Code hook
New-Item -ItemType Directory -Force (Join-Path $out "desktop\windows"), (Join-Path $out "ide\claude-code") | Out-Null
Copy-Item (Join-Path $repo "desktop\windows\*") (Join-Path $out "desktop\windows")
Copy-Item (Join-Path $repo "ide\claude-code\captain_hook.py") (Join-Path $out "ide\claude-code")

# Install-and-test guides shown in the dashboard
New-Item -ItemType Directory -Force (Join-Path $out "docs\guides") | Out-Null
Copy-Item (Join-Path $repo "docs\guides\*.md") (Join-Path $out "docs\guides")

Get-ChildItem -Recurse $out -File | Measure-Object | ForEach-Object { Write-Host "collected $($_.Count) files into $out" }
