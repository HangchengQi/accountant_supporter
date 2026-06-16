param(
    [string]$PythonVersion = "3.12.10",
    [string]$PythonEmbedZipPath = "",
    [string]$OutputRoot = "dist",
    [string]$PackageName = "AccountantSupporterPortable",
    [switch]$NoZip
)

$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$outputRootPath = Join-Path $repoRoot $OutputRoot
$packageRoot = Join-Path $outputRootPath $PackageName
$runtimeRoot = Join-Path $packageRoot "runtime"
$appRoot = Join-Path $packageRoot "app"
$backendTarget = Join-Path $appRoot "backend"
$workflowTarget = Join-Path $appRoot "workflows"
$dataRoot = Join-Path $packageRoot "data"
$cacheRoot = Join-Path $repoRoot "build\cache"

if (Test-Path $packageRoot) {
    Remove-Item -LiteralPath $packageRoot -Recurse -Force
}

New-Item -ItemType Directory -Force -Path $runtimeRoot, $backendTarget, $workflowTarget, $dataRoot | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $dataRoot "bills"), (Join-Path $dataRoot "logs"), (Join-Path $dataRoot "plugin-downloads") | Out-Null

if (-not $PythonEmbedZipPath) {
    New-Item -ItemType Directory -Force -Path $cacheRoot | Out-Null
    $PythonEmbedZipPath = Join-Path $cacheRoot "python-$PythonVersion-embed-amd64.zip"
    if (-not (Test-Path $PythonEmbedZipPath)) {
        $url = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"
        Write-Host "Downloading Python embeddable runtime $PythonVersion..."
        Invoke-WebRequest -Uri $url -OutFile $PythonEmbedZipPath
    }
}

Write-Host "Extracting Python runtime..."
Expand-Archive -LiteralPath $PythonEmbedZipPath -DestinationPath $runtimeRoot -Force

$pthFile = Get-ChildItem -Path $runtimeRoot -Filter "python*._pth" | Select-Object -First 1
if ($pthFile) {
    $pthContent = Get-Content $pthFile.FullName
    if ($pthContent -notcontains "..\app\backend") {
        Add-Content -Path $pthFile.FullName -Value "..\app\backend"
    }
}

Write-Host "Copying application files..."
Copy-Item -Path (Join-Path $repoRoot "backend\app") -Destination $backendTarget -Recurse
Copy-Item -Path (Join-Path $repoRoot "backend\requirements.txt") -Destination $backendTarget
Copy-Item -Path (Join-Path $repoRoot "workflows\*") -Destination $workflowTarget -Recurse
Copy-Item -Path (Join-Path $repoRoot ".env.example") -Destination $packageRoot

$launcher = @'
@echo off
setlocal
set "ROOT=%~dp0"
set "HOST=127.0.0.1"
set "PORT=8080"
set "DATABASE_PATH=%ROOT%data\accountant_support.db"
set "WORKFLOW_PATH=%ROOT%app\workflows\vendor_invoice.v1.json"
set "BILLS_ROOT=%ROOT%data\bills"
set "BILLING_LOGS_ROOT=%ROOT%data\logs"

cd /d "%ROOT%app\backend"
start "" "http://127.0.0.1:8080/"
"%ROOT%runtime\python.exe" -u -m app.main
endlocal
'@
Set-Content -Path (Join-Path $packageRoot "Start Accountant Supporter.bat") -Value $launcher -Encoding ASCII

$readme = @'
Accountant Supporter Portable

How to start:
1. Double-click "Start Accountant Supporter.bat".
2. Your browser should open http://127.0.0.1:8080/.
3. Keep the command window open while using the app.

Local data:
- data\accountant_support.db stores local settings, tokens, queue state, and processed records.
- data\bills stores saved invoice attachments.
- data\logs stores daily billing logs.

Moving the app:
- Copy this whole folder to another Windows PC.
- Do not copy another customer's data folder into this one.

Updating:
- Portable builds created from a ZIP do not need Python installed.
- The in-app update banner only works for installs that are connected to an update source.
'@
Set-Content -Path (Join-Path $packageRoot "README_PORTABLE.txt") -Value $readme -Encoding ASCII

Write-Host "Portable build created:"
Write-Host $packageRoot

if (-not $NoZip) {
    $zipPath = Join-Path $outputRootPath "$PackageName.zip"
    if (Test-Path $zipPath) {
        Remove-Item -LiteralPath $zipPath -Force
    }
    Write-Host "Creating portable ZIP..."
    Compress-Archive -Path $packageRoot -DestinationPath $zipPath -Force
    Write-Host $zipPath
}
