# Build the Windows installer (dist\bin\litcli-<ver>-windows-x86_64-setup.exe).
#
#   pwsh -File scripts\build_binary.ps1 [-Keep]
#
# Freezes litcli with PyInstaller into a throwaway venv, then compiles the Inno Setup script.
# The result installs per-user into %LOCALAPPDATA%\Programs\litcli, adds that to the user PATH,
# and offers the Chromium download as an optional task.
param([switch]$Keep)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$Version = (Select-String -Path "litcli\_version.py" -Pattern '__version__ = "(.*)"').Matches[0].Groups[1].Value
$Work = Join-Path $Root "build\pyinstaller"
$Stage = Join-Path $Root "build\stage"
$Dist = Join-Path $Root "dist\bin"
$Venv = Join-Path $Work "venv"

$Iscc = @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $Iscc) { throw "Inno Setup 6 not found; install it (winget install JRSoftware.InnoSetup) or run this on the CI runner" }

Write-Host "==> clean"
Remove-Item -Recurse -Force $Work, $Stage -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $Work, $Stage, $Dist | Out-Null

Write-Host "==> build environment"
python -m venv $Venv
& "$Venv\Scripts\python.exe" -m pip -q install --upgrade pip
& "$Venv\Scripts\python.exe" -m pip -q install ".[cnki]" pyinstaller   # [cnki]: playwright + jieba, bundled

Write-Host "==> freeze (PyInstaller, onedir)"
& "$Venv\Scripts\pyinstaller.exe" --noconfirm --clean --distpath $Stage --workpath (Join-Path $Work "pyinstaller") packaging\litcli.spec
if ($LASTEXITCODE -ne 0) { throw "pyinstaller failed" }

Write-Host "==> smoke-test the frozen tree"
$exe = Join-Path $Stage "litcli\litcli.exe"
if ((& $exe --version).Trim() -ne "litcli $Version") { throw "frozen build does not answer --version" }
# Backends are imported by name at run time, which is what the spec's hiddenimports is for.
# Ask the registry for every one of them: a module that did not survive the freeze is listed
# as ``[unavailable]``, and a config that failed to load would print a single line instead.
$BackendsToml = Join-Path $Work "backends.toml"
$env:LITCLI_BACKENDS_TOML = $BackendsToml
# A literal here-string: nothing in this program is subject to PowerShell escaping, so the
# TOML it writes stays valid (a double-quoted one turned `"acm"` into `acm"`).
& "$Venv\Scripts\python.exe" -c @'
import os
from litcli.config import _registry
with open(os.environ['LITCLI_BACKENDS_TOML'], 'w', encoding='utf-8') as f:
    for n in sorted(_registry()):
        f.write('[backends.%s]\ntype = "%s"\n' % (n, n))
'@
if ($LASTEXITCODE -ne 0) { throw "cannot enumerate the backend registry" }
if (-not (Test-Path $BackendsToml)) { throw "the backend config was not written" }
$env:LITCLI_CONFIG = $BackendsToml
$backendsOut = & $exe backends list
$unavailable = $backendsOut | Where-Object { $_ -match '\[unavailable\]' }
if ($unavailable) { $unavailable | Write-Host; throw "the frozen build cannot import the backends above (spec hiddenimports?)" }
$want = [int](& "$Venv\Scripts\python.exe" -c "from litcli.config import _registry; print(len(_registry()))")
$have = @($backendsOut | Where-Object { $_ }).Count
if ($have -ne $want) { throw "backends list printed $have rows for $want backends" }
Remove-Item Env:\LITCLI_CONFIG

Write-Host "==> compile the installer"
& $Iscc "/DVersion=$Version" "/DStaging=$(Join-Path $Stage 'litcli')" "packaging\installer\litcli.iss"
if ($LASTEXITCODE -ne 0) { throw "iscc failed" }

Write-Host "==> verify a silent install"
$setup = Join-Path $Dist "litcli-$Version-windows-x86_64-setup.exe"
$Prefix = Join-Path $Work "prefix"
Start-Process -Wait -FilePath $setup -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
    "/DIR=$Prefix", "/TASKS=`"`""
$installed = Join-Path $Prefix "litcli.exe"
if ((& $installed --version).Trim() -ne "litcli $Version") { throw "installed binary does not run" }
& $installed search --help | Out-Null
$uninst = Join-Path $Prefix "unins000.exe"
Start-Process -Wait -FilePath $uninst -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES"
if (Test-Path $installed) { throw "uninstall left the binary behind" }

if (-not $Keep) { Remove-Item -Recurse -Force $Prefix -ErrorAction SilentlyContinue }
Write-Host "==> done: $setup ($([math]::Round((Get-Item $setup).Length / 1MB)) MB)"
