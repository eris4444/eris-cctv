# Eris CCTV installer for Windows - https://github.com/eris4444/eris-cctv
#
#   irm https://raw.githubusercontent.com/eris4444/eris-cctv/main/install.ps1 | iex
#
# Installs into %LOCALAPPDATA%\ErisCCTV (no administrator rights needed), adds the
# `eris-cctv` command and opens the setup menu. Python 3.8+ is installed with winget if missing;
# ffmpeg is downloaded by the setup menu when this PC is set up as a camera.

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$Repo = 'eris4444/eris-cctv'
$Branch = if ($env:ERIS_CCTV_BRANCH) { $env:ERIS_CCTV_BRANCH } else { 'main' }
$Root = Join-Path $env:LOCALAPPDATA 'ErisCCTV'
$App = Join-Path $Root 'app'
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch { }

function Say($t) { Write-Host "  $t" }
function Ok($t) { Write-Host "  [ok] " -ForegroundColor Green -NoNewline; Write-Host $t }
function Die($t) { Write-Host "  [x] $t" -ForegroundColor Red; throw $t }

function Find-Python {
    $cands = @()
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) { $cands += , @($py.Source, '-3') }
    foreach ($n in 'python', 'python3') {
        $c = Get-Command $n -ErrorAction SilentlyContinue
        if ($c -and $c.Source -notlike '*\WindowsApps\*') { $cands += , @($c.Source) }
    }
    foreach ($pattern in "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe",
                         "$env:LOCALAPPDATA\Python\pythoncore-3*\python.exe",
                         "$env:ProgramFiles\Python3*\python.exe") {
        Get-ChildItem $pattern -ErrorAction SilentlyContinue | Sort-Object FullName -Descending |
            ForEach-Object { $cands += , @($_.FullName) }
    }
    foreach ($c in $cands) {
        try {
            $exe = $c[0]; $args2 = @(); if ($c.Count -gt 1) { $args2 = $c[1..($c.Count - 1)] }
            $out = & $exe @args2 -c "import sys; print(sys.executable if sys.version_info >= (3, 8) else '')" 2>$null
            if ($LASTEXITCODE -eq 0 -and $out -and (Test-Path $out.Trim())) { return $out.Trim() }
        } catch { }
    }
    return $null
}

Write-Host ""
Write-Host "  Eris CCTV installer" -ForegroundColor Cyan

$python = Find-Python
if (-not $python) {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        $answer = Read-Host "  Python 3.8+ was not found. Install Python 3.12 now with winget? [Y/n]"
        if ($answer -match '^(|y|yes)$') {
            winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements | Out-Host
            $python = Find-Python
        }
    }
    if (-not $python) { Die "Python 3.8+ is required. Install it from https://www.python.org/downloads/ and run this again." }
}
Ok "Python: $python"

$tmp = Join-Path ([IO.Path]::GetTempPath()) ("eris-cctv-" + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tmp | Out-Null
try {
    if ($env:ERIS_CCTV_SRC) {
        $src = $env:ERIS_CCTV_SRC
    } else {
        Say "Downloading Eris CCTV ($Branch) from GitHub ..."
        $zip = Join-Path $tmp 'src.zip'
        Invoke-WebRequest -UseBasicParsing -Uri "https://codeload.github.com/$Repo/zip/refs/heads/$Branch" -OutFile $zip
        Expand-Archive -Path $zip -DestinationPath $tmp -Force
        $src = (Get-ChildItem $tmp -Directory | Select-Object -First 1).FullName
    }
    if (-not (Test-Path (Join-Path $src 'run.py')) -or -not (Test-Path (Join-Path $src 'eriscctv'))) { Die "Downloaded files look wrong." }

    # stop a running copy so files can be replaced
    foreach ($role in 'agent', 'server') {
        $pidFile = Join-Path $Root "data\$role.pid"
        if (Test-Path $pidFile) {
            $procId = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
            if ($procId) { & taskkill /PID $procId /T /F 2>$null | Out-Null }
        }
    }
    New-Item -ItemType Directory -Force -Path $App | Out-Null
    $pkg = Join-Path $App 'eriscctv'
    if (Test-Path $pkg) { Remove-Item -Recurse -Force $pkg }
    Copy-Item -Recurse (Join-Path $src 'eriscctv') $pkg
    foreach ($f in 'run.py', 'install.ps1', 'README.md', 'LICENSE') {
        $p = Join-Path $src $f
        if (Test-Path $p) { Copy-Item $p (Join-Path $App $f) -Force }
    }
} finally {
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}

$cmdFile = Join-Path $Root 'eris-cctv.cmd'
Set-Content -Path $cmdFile -Encoding ASCII -Value "@echo off`r`n`"$python`" `"%~dp0app\run.py`" %*"
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if (-not $userPath) { $userPath = '' }
if (-not $env:ERIS_CCTV_NO_PATH -and ($userPath -split ';') -notcontains $Root) {
    [Environment]::SetEnvironmentVariable('Path', ($userPath.TrimEnd(';') + ';' + $Root).TrimStart(';'), 'User')
    $env:Path = $env:Path + ';' + $Root
}
Ok "Installed to $App  (command: eris-cctv - open a new terminal to use it)"

# restart copies that are set to start with Windows
$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$pythonw = Join-Path (Split-Path $python) 'pythonw.exe'
if (-not (Test-Path $pythonw)) { $pythonw = $python }
foreach ($item in @(@('agent', 'Eris CCTV camera agent'), @('server', 'Eris CCTV server'))) {
    $v = (Get-ItemProperty -Path $runKey -Name $item[1] -ErrorAction SilentlyContinue).($item[1])
    if ($v) {
        Start-Process -WindowStyle Hidden -FilePath $pythonw -ArgumentList ('"' + (Join-Path $App 'run.py') + '"'), $item[0], 'run'
        Ok "Restarted: $($item[1])"
    }
}

& $python (Join-Path $App 'run.py') @args
