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

$hiddenLauncher = @'
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
backend = fso.BuildPath(root, "app\backend")
python = fso.BuildPath(root, "runtime\pythonw.exe")
If Not fso.FileExists(python) Then
  python = fso.BuildPath(root, "runtime\python.exe")
End If
dataPath = fso.BuildPath(root, "data\accountant_support.db")
workflowPath = fso.BuildPath(root, "app\workflows\vendor_invoice.v1.json")
billsRoot = fso.BuildPath(root, "data\bills")
logsRoot = fso.BuildPath(root, "data\logs")

shell.Environment("PROCESS")("HOST") = "127.0.0.1"
shell.Environment("PROCESS")("PORT") = "8080"
shell.Environment("PROCESS")("DATABASE_PATH") = dataPath
shell.Environment("PROCESS")("WORKFLOW_PATH") = workflowPath
shell.Environment("PROCESS")("BILLS_ROOT") = billsRoot
shell.Environment("PROCESS")("BILLING_LOGS_ROOT") = logsRoot

command = """" & python & """ -u -m app.main"
shell.CurrentDirectory = backend
shell.Run command, 0, False
WScript.Sleep 1500
shell.Run "http://127.0.0.1:8080/", 1, False
'@
Set-Content -Path (Join-Path $packageRoot "Start Accountant Supporter.vbs") -Value $hiddenLauncher -Encoding ASCII

$stopper = @'
@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -Command "$connections = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue; foreach ($connection in $connections) { Stop-Process -Id $connection.OwningProcess -Force -ErrorAction SilentlyContinue }; if (-not $connections) { Write-Host 'Accountant Supporter is not running.' } else { Write-Host 'Accountant Supporter stopped.' }"
pause
endlocal
'@
Set-Content -Path (Join-Path $packageRoot "Stop Accountant Supporter.bat") -Value $stopper -Encoding ASCII

$readme = @'
Accountant Supporter Portable

How to start:
1. Double-click "Start Accountant Supporter.vbs".
2. Your browser should open http://127.0.0.1:8080/.
3. The app runs in the background without a command window.

How to stop:
1. Double-click "Stop Accountant Supporter.bat".
2. Closing the browser tab does not stop the local app by itself.

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
