#Requires -Version 5.1
<#
  Stage the backend into src-tauri/resources/backend so the next
  `tauri build` bundles a fresh, in-sync snapshot of the source.

  Mirrors `backend/` → `src-tauri/resources/backend/`, excluding
  everything the project's .gitignore treats as non-source
  (caches, runtime data, secrets, venv, etc.).

  Usage:
    powershell -ExecutionPolicy Bypass -File .\scripts\sync-backend-resources.ps1
#>
$ErrorActionPreference = "Stop"
Set-Location -Path (Join-Path $PSScriptRoot "..")

$src      = (Resolve-Path ".\backend").Path
$dst      = (Resolve-Path ".\src-tauri\resources\backend").Path

# Patterns to skip — keep in sync with .gitignore for the bits that
# belong to a release bundle.
$skipDirNames = @(
  "__pycache__",
  ".pytest_cache",
  ".mypy_cache",
  ".ruff_cache",
  "node_modules",
  "venv",
  ".venv",
  "data",
  "logs"
)
$skipFileGlobs = @(
  "*.pyc", "*.pyo",
  "*.egg-info",
  "*.db", "*.log",
  ".env", ".env.*"
)

function Test-SkipDir($name) { $skipDirNames -contains $name }
function Test-SkipFile($name) {
  foreach ($g in $skipFileGlobs) {
    if ($name -like $g) { return $true }
  }
  if ($name -like ".env" -or $name -like ".env.*") { return $true }
  return $false
}

function Sync-Dir($s, $d) {
  if (-not (Test-Path $d)) { New-Item -ItemType Directory -Path $d | Out-Null }
  Get-ChildItem -LiteralPath $s -Force | ForEach-Object {
    $name = $_.Name
    $target = Join-Path $d $name
    if ($_.PSIsContainer) {
      if (Test-SkipDir $name) { return }
      Sync-Dir $_.FullName $target
    } else {
      if (Test-SkipFile $name) { return }
      Copy-Item -LiteralPath $_.FullName -Destination $target -Force
    }
  }
}

Write-Host "Syncing $src -> $dst" -ForegroundColor Cyan
Sync-Dir $src $dst
Write-Host "Done." -ForegroundColor Green
