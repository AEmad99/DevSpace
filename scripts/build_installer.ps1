#Requires -Version 5.1
<#
  Stage a self-contained, relocatable CPython + every backend pip dependency
  into src-tauri/resources/python/, then build the Tauri NSIS installer.

  Pipeline:
    1. uv venv    -> creates an isolated CPython at src-tauri/resources/python-venv
    2. uv pip install -r backend/requirements.txt
                 -> pulls every backend dep (FastAPI, ChromaDB, fastembed, ...)
    3. Strip __pycache__, *.pyc, pip, setuptools, wheel, ...
                 -> shrinks ~350 MB -> ~250 MB without losing any runtime
    4. Mirror backend/ -> src-tauri/resources/backend/
    5. tauri build  -> produces NSIS installer under
                 src-tauri/target/release/bundle/nsis/

  The resulting installer is fully self-contained: it embeds its own Python
  interpreter + every backend dependency + the backend source, so it runs on
  any Windows machine with no dev-machine dependency.

  Usage:
    powershell -ExecutionPolicy Bypass -File .\scripts\build_installer.ps1
      [-SkipDeps]   skip the (slow) uv pip install step -- use only if the venv
                    is already up to date with backend/requirements.txt
      [-SkipBuild]  stage resources but do not invoke tauri build
      [-Clean]      wipe src-tauri/resources/python-venv before building

  Requirements on PATH: uv (https://github.com/astral-sh/uv), cargo,
  the Tauri CLI (cargo install tauri-cli --version ^2).
#>

[CmdletBinding()]
param(
    [switch]$SkipDeps,
    [switch]$SkipBuild,
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
Set-Location -Path (Join-Path $PSScriptRoot "..")

$repoRoot      = (Get-Location).Path
$backendDir    = Join-Path $repoRoot "backend"
$requirements  = Join-Path $backendDir "requirements.txt"
$tauriDir      = Join-Path $repoRoot "src-tauri"
$resourcesDir  = Join-Path $tauriDir "resources"
$stagingVenv   = Join-Path $resourcesDir "python-venv"
$targetPyDir   = Join-Path $resourcesDir "python"
$targetBackend = Join-Path $resourcesDir "backend"

function Assert-Command($name) {
    if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
        throw "Required command '$name' is not on PATH. See script header for prerequisites."
    }
}

function Format-Elapsed($ts) {
    $elapsed = (Get-Date) - $ts
    return "{0:mm}m{0:ss}s" -f $elapsed
}

# ----- Pre-flight -------------------------------------------------------
Assert-Command "uv"
Assert-Command "cargo"
# Tauri CLI is normally installed via `cargo install tauri-cli --version ^2`
# (cargo tauri build) OR via npm (tauri build). Try the npm one first since
# it's typically what's on a dev's machine, and fall back to cargo.
$tauriCmd = $null
if (Get-Command "tauri" -ErrorAction SilentlyContinue) {
    $tauriCmd = { & tauri build @args }
} elseif (Get-Command "cargo-tauri" -ErrorAction SilentlyContinue) {
    $tauriCmd = { & cargo tauri build @args }
} else {
    throw "Tauri CLI not found. Install via `npm install -g @tauri-apps/cli` or `cargo install tauri-cli --version ^2`."
}

# Python version pinned in pyproject.toml / docs; 3.11 is what the bundled
# app targets and what the on-disk test env uses today. Bump together if
# the project's minimum changes.
$pythonVersion = "3.11"

Write-Host ""
Write-Host "+================================================================+"
Write-Host "|            DevSpace -- All-In-One Installer Build             |"
Write-Host "+================================================================+"
Write-Host ""
Write-Host ("Repo root        : {0}" -f $repoRoot)
Write-Host ("Backend          : {0}" -f $backendDir)
Write-Host ("Requirements     : {0}" -f $requirements)
Write-Host ("Staging venv     : {0}" -f $stagingVenv)
Write-Host ("Target Python    : {0}" -f $targetPyDir)
Write-Host ("Target backend   : {0}" -f $targetBackend)
Write-Host ("Python version   : {0}" -f $pythonVersion)
Write-Host ("Skip deps        : {0}" -f $SkipDeps)
Write-Host ("Skip tauri build : {0}" -f $SkipBuild)
Write-Host ""

# ----- Step 1+2: Build the Python venv + install deps -------------------
$venvTs = Get-Date
if ($Clean -and (Test-Path $stagingVenv)) {
    Write-Host "[1/5] Removing previous staging venv (--Clean)..." -ForegroundColor Yellow
    Remove-Item -LiteralPath $stagingVenv -Recurse -Force
}

$needDeps = $true
if ($SkipDeps) {
    $needDeps = $false
    Write-Host "[1/5] Skipping venv + deps (--SkipDeps)" -ForegroundColor Yellow
}

# Cheap freshness check: only re-install when requirements.txt mtime is newer
# than the staging venv marker. Saves ~5 minutes on repeat builds with
# unchanged deps. Only relevant when deps were not already skipped above.
if ($needDeps) {
    $venvCfgPath = Join-Path $stagingVenv "pyvenv.cfg"
    $venvIsFresh = $false
    if ((Test-Path $stagingVenv) -and (Test-Path $venvCfgPath)) {
        $reqMtime  = (Get-Item $requirements).LastWriteTimeUtc
        $venvMtime = (Get-Item $venvCfgPath).LastWriteTimeUtc
        if ($venvMtime -ge $reqMtime) {
            $venvIsFresh = $true
        }
    }
    if ($venvIsFresh) {
        Write-Host "[1/5] Staging venv is up to date -- reusing." -ForegroundColor Green
        $needDeps = $false
    }
}

if ($needDeps) {
    Write-Host "[1/5] Creating CPython $pythonVersion venv at $stagingVenv..." -ForegroundColor Green
    if (Test-Path $stagingVenv) {
        Remove-Item -LiteralPath $stagingVenv -Recurse -Force
    }
    & uv venv --python $pythonVersion $stagingVenv
    if ($LASTEXITCODE -ne 0) { throw "uv venv failed (exit $LASTEXITCODE)" }

    Write-Host ""
    Write-Host "[2/5] Installing backend requirements (this can take several minutes)..." -ForegroundColor Green
    & uv pip install --python "$stagingVenv\Scripts\python.exe" -r $requirements
    if ($LASTEXITCODE -ne 0) { throw "uv pip install failed (exit $LASTEXITCODE)" }
}
Write-Host ("    venv stage elapsed: {0}" -f (Format-Elapsed $venvTs)) -ForegroundColor DarkGray

# ----- Relocate stdlib so the venv is portable ----------------------------
# uv venv stores a pyvenv.cfg that hardcodes the original CPython home
# directory (e.g. %APPDATA%\uv\python\cpython-3.11-...-none) and uses
# symlinks under the hood. The installer would only run on the build
# machine. We fix this by:
#   1. Copying the stdlib (Lib/) from the uv-managed CPython into the
#      staging venv so all stdlib modules are physically present.
#   2. Copying DLLs/libs/include/tcl for fully relocatable behaviour.
#   3. Replacing pyvenv.cfg with a python._pth that pins sys.path
#      relative to the install location, ignoring the original home.
#
# On --SkipDeps we operate on the already-published $targetPyDir
# (the prior build's output) so the .pth update applies; on a fresh
# build we work on $stagingVenv and step 4 moves the result.
$relocateTs = Get-Date
Write-Host ""
Write-Host "[2.5/5] Relocating stdlib so the venv is portable..." -ForegroundColor Green

$relocateRoot = if ($SkipDeps) { $targetPyDir } else { $stagingVenv }
Write-Host ("    relocate target: {0}" -f $relocateRoot)

# Read pyvenv.cfg to find the source CPython home. On --SkipDeps there
# may be no pyvenv.cfg left (it was rewritten to python._pth in the
# prior build); fall back to the well-known uv install path. If THAT
# is also gone, the build machine doesn't have uv and the user must
# re-run with -Clean (which will rebuild + relocate).
$venvCfgPath = Join-Path $relocateRoot "pyvenv.cfg"
$srcPyHome = $null
if (Test-Path $venvCfgPath) {
    $venvCfg = Get-Content -Raw -LiteralPath $venvCfgPath
    $homeLine = $venvCfg -split "`n" | Where-Object { $_.StartsWith("home = ") } | Select-Object -First 1
    if ($homeLine) {
        $srcPyHome = ($homeLine -replace "^home = ", "").Trim()
    }
}
if (-not $srcPyHome) {
    $srcPyHome = Join-Path $env:USERPROFILE "AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none"
}
if (-not (Test-Path $srcPyHome)) {
    throw ("Source CPython home {0} not found; re-run with -Clean to rebuild from scratch." -f $srcPyHome)
}
Write-Host ("    source CPython: {0}" -f $srcPyHome)

# Copy stdlib tree, DLLs, libs, include, tcl. We DON'T copy python.exe /
# python311.dll / python3.dll -- those are unique to the source and we
# can't redistribute them. Instead we point the venv at the uv-managed
# python.exe via python._pth, OR we ship the embeddable distribution.
#
# Simpler: copy python.exe / python3.dll / python311.dll from the source
# into the venv. They're part of the same CPython install, the build
# machine has them, and shipping them is the standard practice for any
# "all-in-one" Python bundler (PyInstaller, Briefcase, etc.).
#
# IMPORTANT: copy each stdlib subdirectory individually so we do NOT
# clobber the venv's `Lib/site-packages/` (which holds all the backend
# deps we just installed). The source's `Lib/site-packages` is the
# system Python's empty site-packages; copying the whole `Lib/` would
# erase the deps. Iterate the source's top-level subdirs (excluding
# `site-packages`) and merge them into the venv's Lib.
$srcLib = Join-Path $srcPyHome "Lib"
$dstLib = Join-Path $relocateRoot "Lib"
if (Test-Path $srcLib) {
    foreach ($entry in (Get-ChildItem -LiteralPath $srcLib -Force)) {
        if ($entry.Name -eq "site-packages") { continue }
        $dst = Join-Path $dstLib $entry.Name
        if (Test-Path $dst) { Remove-Item -LiteralPath $dst -Recurse -Force }
        Copy-Item -LiteralPath $entry.FullName -Destination $dst -Recurse -Force
    }
}
foreach ($sub in @("DLLs", "libs", "include", "tcl")) {
    $src = Join-Path $srcPyHome $sub
    $dst = Join-Path $relocateRoot $sub
    if (Test-Path $src) {
        if (Test-Path $dst) { Remove-Item -LiteralPath $dst -Recurse -Force }
        Copy-Item -LiteralPath $src -Destination $dst -Recurse -Force
    }
}
# Copy the interpreter binaries (python.exe, python3.dll, python311.dll,
# vcruntime*.dll). These MUST match the stdlib version exactly, so they
# always come from the same uv-managed CPython home.
foreach ($name in @("python.exe", "python3.dll", "python311.dll", "vcruntime140.dll", "vcruntime140_1.dll")) {
    $src = Join-Path $srcPyHome $name
    if (Test-Path $src) {
        Copy-Item -LiteralPath $src -Destination (Join-Path $relocateRoot $name) -Force
    }
}

# Drop the original pyvenv.cfg and write a python._pth instead. python._pth
# is read BEFORE pyvenv.cfg, and its presence disables the latter's `home`
# lookup. Lines starting with `#` are comments; we use it to pin sys.path
# to the venv's own Lib and site-packages so the interpreter finds the
# bundled stdlib regardless of where it ends up on the user's machine.
#
# Note: kept ASCII-only on purpose. PowerShell 5.1's here-string + UTF-8
# output pipeline mangles non-ASCII chars, and a mojibake em-dash here
# would crash the embedded interpreter at startup. ASCII is bulletproof.
$pthPath = Join-Path $relocateRoot "python._pth"
$pthContent = @"
# python._pth - pins the stdlib + site-packages search paths so the
# interpreter runs from THIS directory regardless of where the user
# installs the app. Without this, pyvenv.cfg would point at the build
# machine's CPython home and the app would only run on the build box.
#
# The `.` line means "look in the directory containing this file for
# python311.dll" - equivalent to setting PYTHONHOME to the install root.
.
# `import site` enables site-packages processing (.pth files etc.)
import site

# Pin the stdlib + our bundled deps. Relative to the venv root so the
# whole tree relocates as a unit. We list Lib AND DLLs because Python
# normally auto-adds DLLs/ to the path for built-in extensions like
# _socket.pyd, but with python._pth present the auto-include is OFF
# and we have to add it explicitly or `import socket` fails with
# "No module named '_socket'" even though _socket.pyd is sitting right
# there in DLLs/.
Lib
DLLs
Lib\site-packages

# Disable user site - we're a frozen, shipped app, no per-user site-packages.
# (The "nosite" option in python._pth already implies this, but stating it
# explicitly makes the intent obvious.)
"@
[System.IO.File]::WriteAllText($pthPath, $pthContent, [System.Text.UTF8Encoding]::new($false))
Remove-Item -LiteralPath $venvCfgPath -Force -ErrorAction SilentlyContinue

Write-Host ("    relocate elapsed: {0}" -f (Format-Elapsed $relocateTs)) -ForegroundColor DarkGray

# ----- Step 3: Strip unnecessary files from the venv ---------------------
$stripTs = Get-Date
Write-Host ""
Write-Host "[3/5] Stripping pycache + pip cache + test-only deps..." -ForegroundColor Green

# python.exe lives under Scripts/ on Windows. Lib/ holds stdlib;
# Lib/site-packages/ holds our deps. Everything else (include/, share/,
# tcl/, ...) is unnecessary overhead we can drop.
#
# When --SkipDeps is set we strip from the previously-published targetPyDir
# directly; otherwise we strip from the just-built stagingVenv (which step 4
# then moves into targetPyDir).
$stripRoot = if ($SkipDeps) { $targetPyDir } else { $stagingVenv }
$srcLib = Join-Path $stripRoot "Lib"
if (-not (Test-Path $srcLib)) {
    throw ("Expected {0} -- venv layout not what uv produces?" -f $srcLib)
}

# Drop __pycache__/ and .pyc files (rebuilt at import time on first run).
Get-ChildItem -LiteralPath $srcLib -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Get-ChildItem -LiteralPath $srcLib -Recurse -File -Filter "*.pyc" -ErrorAction SilentlyContinue |
    Remove-Item -Force -ErrorAction SilentlyContinue

# Drop pip + setuptools + wheel: the app never installs packages, so we
# don't need the package manager. Saves ~10 MB and avoids shipping a
# known-vulnerable pip version frozen at build time. ALSO drop any .pth
# files that import these (e.g. distutils-precedence.pth imports
# _distutils_hack; if we deleted the dir but left the .pth, Python prints
# an error on every startup and "Remainder of file ignored" for the rest
# of sitecustomize -- cosmetic but ugly in the splash window).
$sitePackages = Join-Path $srcLib "site-packages"
$pipDirs = @("pip", "pip-*", "setuptools", "setuptools-*", "wheel", "wheel-*", "pkg_resources", "_distutils_hack")
$pipPthFiles = @("distutils-precedence.pth")
if (Test-Path $sitePackages) {
    foreach ($d in (Get-ChildItem -LiteralPath $sitePackages -Directory -ErrorAction SilentlyContinue)) {
        foreach ($g in $pipDirs) {
            if ($d.Name -like $g) {
                Remove-Item -LiteralPath $d.FullName -Recurse -Force -ErrorAction SilentlyContinue
                break
            }
        }
    }
    foreach ($f in (Get-ChildItem -LiteralPath $sitePackages -File -ErrorAction SilentlyContinue)) {
        foreach ($g in $pipDirs) {
            if ($f.Name -like $g) {
                Remove-Item -LiteralPath $f.FullName -Force -ErrorAction SilentlyContinue
            }
        }
        foreach ($g in $pipPthFiles) {
            if ($f.Name -eq $g) {
                Remove-Item -LiteralPath $f.FullName -Force -ErrorAction SilentlyContinue
            }
        }
    }
}

Write-Host ("    strip elapsed: {0}" -f (Format-Elapsed $stripTs)) -ForegroundColor DarkGray

# ----- Step 4: Move staging venv into the resources tree -----------------
# Only meaningful when we just built a fresh staging venv; skip when
# --SkipDeps is set because the resources tree is already in place.
$moveTs = Get-Date
if (-not $SkipDeps) {
    Write-Host ""
    Write-Host "[4/5] Publishing venv to $targetPyDir..." -ForegroundColor Green
    if (Test-Path $targetPyDir) {
        Remove-Item -LiteralPath $targetPyDir -Recurse -Force
    }
    # Rename in-place is faster than copy on the same volume.
    Move-Item -LiteralPath $stagingVenv -Destination $targetPyDir
    Write-Host ("    move elapsed: {0}" -f (Format-Elapsed $moveTs)) -ForegroundColor DarkGray
} else {
    Write-Host ""
    Write-Host "[4/5] Skipping move (--SkipDeps); reusing existing $targetPyDir." -ForegroundColor DarkGray
}

# ----- Step 5: Sync backend source ---------------------------------------
$syncTs = Get-Date
Write-Host ""
Write-Host "[5/5] Syncing backend source to $targetBackend..." -ForegroundColor Green
& (Join-Path $repoRoot "scripts\sync-backend-resources.ps1")
if (-not $?) { throw "backend sync failed" }
Write-Host ("    sync elapsed: {0}" -f (Format-Elapsed $syncTs)) -ForegroundColor DarkGray

# ----- Size summary ------------------------------------------------------
$pySize = (Get-ChildItem -LiteralPath $targetPyDir -Recurse -File -ErrorAction SilentlyContinue |
    Measure-Object -Property Length -Sum).Sum
$beSize = (Get-ChildItem -LiteralPath $targetBackend -Recurse -File -ErrorAction SilentlyContinue |
    Measure-Object -Property Length -Sum).Sum
Write-Host ""
Write-Host "Bundle sizes:" -ForegroundColor Cyan
Write-Host ("  python    : {0,8:N1} MB  ({1})" -f ($pySize / 1MB), $targetPyDir)
Write-Host ("  backend   : {0,8:N1} MB  ({1})" -f ($beSize / 1MB), $targetBackend)

# ----- Build the Tauri installer -----------------------------------------
if ($SkipBuild) {
    Write-Host ""
    Write-Host "[tauri] Skipped (--SkipBuild). Run 'tauri build' to produce the installer." -ForegroundColor Yellow
    exit 0
}

Write-Host ""
Write-Host "[tauri] Building NSIS installer (this can take 15-20 minutes for a cold Rust build)..." -ForegroundColor Green
$tauriTs = Get-Date
Push-Location $tauriDir
try {
    & $tauriCmd
    if ($LASTEXITCODE -ne 0) { throw "tauri build failed (exit $LASTEXITCODE)" }
} finally {
    Pop-Location
}

# ----- Locate the output -------------------------------------------------
$nsisDir = Join-Path $tauriDir "target\release\bundle\nsis"
if (Test-Path $nsisDir) {
    $installer = Get-ChildItem -LiteralPath $nsisDir -Filter "*.exe" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($installer) {
        Write-Host ""
        Write-Host "+================================================================+"
        Write-Host "|                  OK  Installer ready                          |"
        Write-Host "+================================================================+"
        Write-Host ""
        Write-Host ("  Path : {0}" -f $installer.FullName) -ForegroundColor Green
        Write-Host ("  Size : {0:N1} MB" -f ($installer.Length / 1MB))
        Write-Host ("  Time : {0}" -f (Format-Elapsed $tauriTs))
    }
}