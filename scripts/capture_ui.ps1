[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [string]$OutputPath = (Join-Path $PSScriptRoot "..\tests\visual\candidates"),
    [Parameter(Mandatory = $false)]
    [string[]]$Views = @("mods", "import", "nexus", "settings", "credits"),
    [Parameter(Mandatory = $false)]
    [string[]]$Sizes = @("1600x1000", "1280x820", "960x640"),
    [Parameter(Mandatory = $false)]
    [ValidateRange(5, 120)]
    [int]$TimeoutSeconds = 30
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$fixtureServer = Join-Path $repoRoot "tests\visual\fixture_server.py"
$allowedViews = @("mods", "import", "nexus", "settings", "credits")
$allowedSizes = @("1600x1000", "1280x820", "960x640")
foreach ($view in $Views) {
    if ($allowedViews -notcontains $view) { throw "Unknown view: $view" }
}
foreach ($size in $Sizes) {
    if ($allowedSizes -notcontains $size) { throw "Unknown size: $size" }
}

function Find-Edge {
    $command = Get-Command "msedge.exe" -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    $locations = @(
        (Join-Path $env:ProgramFiles "Microsoft\Edge\Application\msedge.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Microsoft\Edge\Application\msedge.exe"),
        (Join-Path $env:LOCALAPPDATA "Microsoft\Edge\Application\msedge.exe")
    )
    foreach ($location in $locations) {
        if (Test-Path $location -PathType Leaf) { return $location }
    }
    throw "Microsoft Edge (msedge.exe) was not found"
}

function Find-Python {
    $venv = Join-Path $repoRoot ".venv-build\Scripts\python.exe"
    if (Test-Path $venv -PathType Leaf) { return $venv }
    foreach ($name in @("py.exe", "python.exe")) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command) { return $command.Source }
    }
    throw "Python was not found"
}

function Quote-NativeArgument([string]$Value) {
    if ($Value.Contains('"')) { throw "Native argument contains a quote" }
    if ($Value -notmatch '[\s]') { return $Value }
    return '"' + $Value + '"'
}

function Join-NativeArguments([string[]]$Values) {
    return (($Values | ForEach-Object { Quote-NativeArgument $_ }) -join " ")
}

function Start-NativeProcess([string]$FilePath, [string[]]$Arguments) {
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $FilePath
    $info.Arguments = Join-NativeArguments $Arguments
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    return [System.Diagnostics.Process]::Start($info)
}

function Stop-ProcessTree($Process) {
    if (-not $Process) { return }
    try {
        if (-not $Process.HasExited) {
            & taskkill.exe /PID $Process.Id /T /F 2>$null | Out-Null
            $Process.WaitForExit(5000) | Out-Null
        }
    }
    catch {
        Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
    }
}

$edge = Find-Edge
$python = Find-Python
$outputDirectory = [System.IO.Path]::GetFullPath($OutputPath)
New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
$runId = [Guid]::NewGuid().ToString("N")
$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) "paldeck visual $runId"
$portFile = Join-Path $tempRoot "fixture.port"
$readyDirectory = Join-Path $tempRoot "ready markers"
$userDataRoot = Join-Path $tempRoot "edge profiles"
New-Item -ItemType Directory -Path $readyDirectory -Force | Out-Null
New-Item -ItemType Directory -Path $userDataRoot -Force | Out-Null
$server = $null
$edgeProcess = $null

try {
    $pythonArguments = @()
    if ([System.IO.Path]::GetFileName($python).Equals("py.exe", [System.StringComparison]::OrdinalIgnoreCase)) {
        $pythonArguments += "-3.13"
    }
    $pythonArguments += @("-u", $fixtureServer, "--port", "0", "--ready-file", $portFile, "--ready-dir", $readyDirectory)
    $server = Start-NativeProcess $python $pythonArguments

    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    while (-not (Test-Path $portFile -PathType Leaf)) {
        if ($server.HasExited) { throw "Visual fixture server exited with code $($server.ExitCode)" }
        if ([DateTime]::UtcNow -ge $deadline) { throw "Visual fixture server start timed out" }
        Start-Sleep -Milliseconds 50
    }
    $port = (Get-Content $portFile -Raw).Trim()
    if ($port -notmatch "^\d+$") { throw "Visual fixture returned an invalid port: $port" }

    foreach ($view in $Views) {
        foreach ($size in $Sizes) {
            $parts = $size.Split("x")
            $screenshot = Join-Path $outputDirectory "$view-$size.png"
            $profile = Join-Path $userDataRoot "$view-$size"
            $captureToken = [Guid]::NewGuid().ToString("N")
            $readyMarker = Join-Path $readyDirectory "$captureToken.ready"
            New-Item -ItemType Directory -Path $profile -Force | Out-Null
            Remove-Item $screenshot -Force -ErrorAction SilentlyContinue
            Remove-Item $readyMarker -Force -ErrorAction SilentlyContinue
            $url = "http://127.0.0.1:$port/index.html?view=$view&capture=$captureToken"
            $arguments = @(
                "--headless=new",
                "--disable-gpu",
                "--hide-scrollbars",
                "--run-all-compositor-stages-before-draw",
                "--virtual-time-budget=3000",
                "--window-size=$($parts[0]),$($parts[1])",
                "--force-device-scale-factor=1",
                "--user-data-dir=$profile",
                "--screenshot=$screenshot",
                $url
            )
            try {
                $edgeProcess = Start-NativeProcess $edge $arguments
                if (-not $edgeProcess.WaitForExit($TimeoutSeconds * 1000)) {
                    throw "Edge screenshot timed out: $view $size"
                }
                if ($edgeProcess.ExitCode -ne 0) { throw "Edge screenshot failed: $view $size (exit $($edgeProcess.ExitCode))" }
            }
            finally {
                Stop-ProcessTree $edgeProcess
                $edgeProcess = $null
            }
            if (-not (Test-Path $readyMarker -PathType Leaf) -or (Get-Content $readyMarker -Raw).Trim() -ne $view) {
                throw "Visual ready handshake failed: $view $size"
            }
            if (-not (Test-Path $screenshot -PathType Leaf) -or (Get-Item $screenshot).Length -eq 0) {
                throw "Edge did not produce a screenshot: $screenshot"
            }
            Write-Host "Captured $view $size -> $screenshot"
        }
    }
}
finally {
    Stop-ProcessTree $edgeProcess
    Stop-ProcessTree $server
    Remove-Item $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
}
