#Requires -Version 7.0

[CmdletBinding()]
param(
    [string]$InstallRoot = (Join-Path ([System.IO.Path]::GetTempPath()) 'slagalpha-python-31213')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$pythonVersion = '3.12.13'
$uvVersion = '0.12.10'
$uvArchiveSha256 = 'f65744f94072152b1f86ba2aace4d01f1124d9a8ecb235805039e3718c36cac2'
$uvExeSha256 = 'a8bf95637ba520491de06713d718a55b90f18d127980b9531fd8fc5a8e99dc1d'
$environmentLockSha256 = '8c3d1ef3887544516ac06fa3efe7f9bcfc2b81b1f56b1da267cafa6a24574ee7'
$dependencyManifestHash = '060d25d955664a803bbdc39b1eecbd466cb57a4a9c5ee278c9c15cb4358bc567'
$dependencyManifestSha256 = '3740153d436cc461306f8027359d9c50de367e0c2c9f0b2c16e0c70f964a0bca'

function Assert-NotReparsePoint {
    param([string]$Path, [string]$Label)
    if ((Test-Path -LiteralPath $Path) -and
        ((Get-Item -LiteralPath $Path -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
        throw "$Label cannot be a reparse point: $Path"
    }
}

$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$installRoot = [System.IO.Path]::GetFullPath($InstallRoot)
$pathComparison = [System.StringComparison]::OrdinalIgnoreCase
if ($installRoot.Equals($projectRoot, $pathComparison) -or
    $installRoot.StartsWith($projectRoot + [System.IO.Path]::DirectorySeparatorChar, $pathComparison)) {
    throw 'InstallRoot must be outside the project so the frozen environment cannot enter Git.'
}
Assert-NotReparsePoint $installRoot 'InstallRoot'
New-Item -ItemType Directory -Path $installRoot -Force | Out-Null

function Assert-FileHash {
    param([string]$Path, [string]$Expected, [string]$Label)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label is missing: $Path"
    }
    $observed = (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
    if ($observed -ne $Expected) {
        throw "$Label SHA-256 mismatch: $observed"
    }
}

$lockPath = Join-Path $projectRoot 'requirements.lock'
$manifestPath = Join-Path $projectRoot "data/manifests/dependency_artifacts/$dependencyManifestHash.json"
$wheelDirectory = Join-Path $projectRoot 'data/dependency-artifacts'
Assert-FileHash $lockPath $environmentLockSha256 'environment lock'
Assert-FileHash $manifestPath $dependencyManifestSha256 'dependency manifest'
if (-not (Test-Path -LiteralPath $wheelDirectory -PathType Container)) {
    throw "frozen wheel directory is missing: $wheelDirectory"
}

$uvArchive = Join-Path $installRoot 'uv-x86_64-pc-windows-msvc.zip'
$uvDirectory = Join-Path $installRoot 'uv'
$uvExe = Join-Path $uvDirectory 'uv.exe'
Assert-NotReparsePoint $uvDirectory 'uv directory'
if (-not (Test-Path -LiteralPath $uvArchive)) {
    $download = Join-Path $installRoot ("uv-{0}.download" -f [guid]::NewGuid().ToString('N'))
    $uri = "https://github.com/astral-sh/uv/releases/download/$uvVersion/uv-x86_64-pc-windows-msvc.zip"
    Invoke-WebRequest -Uri $uri -OutFile $download
    Assert-FileHash $download $uvArchiveSha256 'downloaded uv archive'
    Move-Item -LiteralPath $download -Destination $uvArchive
}
Assert-FileHash $uvArchive $uvArchiveSha256 'uv archive'
if (-not (Test-Path -LiteralPath $uvExe)) {
    if (Test-Path -LiteralPath $uvDirectory) {
        throw "partial uv extraction exists: $uvDirectory"
    }
    Expand-Archive -LiteralPath $uvArchive -DestinationPath $uvDirectory
}
Assert-FileHash $uvExe $uvExeSha256 'uv executable'
$observedUvVersion = (& $uvExe --version)
if ($LASTEXITCODE -ne 0 -or $observedUvVersion -notmatch "^uv $([regex]::Escape($uvVersion)) ") {
    throw "unexpected uv version: $observedUvVersion"
}

$pythonDirectory = Join-Path $installRoot 'python'
$pythonExe = Join-Path $pythonDirectory "cpython-$pythonVersion-windows-x86_64-none/python.exe"
Assert-NotReparsePoint $pythonDirectory 'Python directory'
if (-not (Test-Path -LiteralPath $pythonExe)) {
    & $uvExe python install $pythonVersion --install-dir $pythonDirectory --no-bin --no-registry --no-cache --no-config
    if ($LASTEXITCODE -ne 0) {
        throw 'uv failed to install the frozen Python runtime.'
    }
}
$observedPythonVersion = (& $pythonExe -c 'import platform; print(platform.python_version())')
if ($LASTEXITCODE -ne 0 -or $observedPythonVersion -ne $pythonVersion) {
    throw "unexpected Python version: $observedPythonVersion"
}

$venvDirectory = Join-Path $installRoot 'venv'
$venvPython = Join-Path $venvDirectory 'Scripts/python.exe'
Assert-NotReparsePoint $venvDirectory 'virtual environment directory'
if (-not (Test-Path -LiteralPath $venvPython)) {
    & $pythonExe -m venv $venvDirectory
    if ($LASTEXITCODE -ne 0) {
        throw 'failed to create the frozen virtual environment.'
    }
}
$venvVersion = (& $venvPython -c 'import platform; print(platform.python_version())')
if ($LASTEXITCODE -ne 0 -or $venvVersion -ne $pythonVersion) {
    throw "existing virtual environment uses Python $venvVersion instead of $pythonVersion"
}

$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
$wheelFiles = @(Get-ChildItem -LiteralPath $wheelDirectory -File -Filter '*.whl')
if ($wheelFiles.Count -ne $manifest.wheels.Count) {
    throw 'local wheel count differs from the frozen dependency manifest.'
}
$hashedRequirements = Join-Path $installRoot "dependencies-$dependencyManifestHash.lock"
$lines = @($manifest.wheels | ForEach-Object {
    "{0}=={1} --hash=sha256:{2}" -f $_.name, $_.version, $_.sha256
})
Assert-NotReparsePoint $hashedRequirements 'generated requirements file'
[System.IO.File]::WriteAllLines(
    $hashedRequirements, $lines, [System.Text.UTF8Encoding]::new($false)
)
& $venvPython -m pip --isolated install --no-index --find-links $wheelDirectory `
    --only-binary=:all: --require-hashes --disable-pip-version-check -r $hashedRequirements
if ($LASTEXITCODE -ne 0) {
    throw 'offline installation from frozen wheels failed.'
}

$verification = @'
import sys
from pathlib import Path
from slagalpha.reporting.dependency_artifacts import DependencyArtifactManifest, inspect_dependency_artifacts
from slagalpha.reporting.environment import inspect_environment_lock

root = Path(sys.argv[1])
digest = sys.argv[2]
lock = root / "requirements.lock"
manifest = DependencyArtifactManifest.model_validate_json(
    (root / "data" / "manifests" / "dependency_artifacts" / f"{digest}.json").read_bytes()
)
inspect_dependency_artifacts(project_dir=root, environment_lock=lock.read_bytes(), manifest=manifest)
print(inspect_environment_lock(lock))
'@
$previousPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = Join-Path $projectRoot 'src'
    $verifiedLockHash = (& $venvPython -c $verification $projectRoot $dependencyManifestHash)
    if ($LASTEXITCODE -ne 0 -or $verifiedLockHash -ne $environmentLockSha256) {
        throw "frozen environment verification failed: $verifiedLockHash"
    }
    & $venvPython -m pip check
    if ($LASTEXITCODE -ne 0) {
        throw 'pip check failed in the frozen environment.'
    }
}
finally {
    $env:PYTHONPATH = $previousPythonPath
}

[ordered]@{
    status = 'READY'
    python_version = $pythonVersion
    uv_version = $uvVersion
    environment_lock_sha256 = $verifiedLockHash
    dependency_manifest_hash = $dependencyManifestHash
    venv_python = $venvPython
    research_authorized = $false
} | ConvertTo-Json -Compress
