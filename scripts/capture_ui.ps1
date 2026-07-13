[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [string]$OutputPath = (Join-Path $PSScriptRoot "..\tests\visual\candidates")
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$fixtureServer = Join-Path $repoRoot "tests\visual\fixture_server.py"
$views = @("mods", "import", "nexus", "settings", "credits")
$sizes = @("1600x1000", "1280x820", "960x640")

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
    throw "未找到本机 Microsoft Edge (msedge.exe)"
}

function Find-Python {
    foreach ($name in @("py.exe", "python.exe")) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command) { return $command.Source }
    }
    throw "未找到 Python，无法启动视觉夹具服务"
}

$edge = Find-Edge
$python = Find-Python
$outputDirectory = [System.IO.Path]::GetFullPath($OutputPath)
New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
$runId = [Guid]::NewGuid().ToString("N")
$readyFile = Join-Path ([System.IO.Path]::GetTempPath()) "paldeck-visual-$runId.port"
$userDataRoot = Join-Path ([System.IO.Path]::GetTempPath()) "paldeck-edge-$runId"
$server = $null

try {
    $pythonArguments = @()
    if ([System.IO.Path]::GetFileName($python).Equals("py.exe", [System.StringComparison]::OrdinalIgnoreCase)) {
        $pythonArguments += "-3.13"
    }
    $pythonArguments += @("-u", $fixtureServer, "--port", "0", "--ready-file", $readyFile)
    $server = Start-Process -FilePath $python -ArgumentList $pythonArguments -PassThru -WindowStyle Hidden

    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    while (-not (Test-Path $readyFile -PathType Leaf)) {
        if ($server.HasExited) { throw "视觉夹具服务启动失败（exit $($server.ExitCode)）" }
        if ([DateTime]::UtcNow -ge $deadline) { throw "等待视觉夹具服务随机端口超时" }
        Start-Sleep -Milliseconds 50
    }
    $port = (Get-Content $readyFile -Raw).Trim()
    if ($port -notmatch "^\d+$") { throw "视觉夹具服务返回了无效端口：$port" }

    foreach ($view in $views) {
        foreach ($size in $sizes) {
            $parts = $size.Split("x")
            $screenshot = Join-Path $outputDirectory "$view-$size.png"
            $profile = Join-Path $userDataRoot "$view-$size"
            New-Item -ItemType Directory -Path $profile -Force | Out-Null
            if (Test-Path $screenshot) { Remove-Item $screenshot -Force }
            $url = "http://127.0.0.1:$port/index.html?view=$view"
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
            $process = Start-Process -FilePath $edge -ArgumentList $arguments -PassThru -Wait
            if ($process.ExitCode -ne 0) { throw "Edge 截图失败：$view $size（exit $($process.ExitCode)）" }
            if (-not (Test-Path $screenshot -PathType Leaf) -or (Get-Item $screenshot).Length -eq 0) {
                throw "Edge 未生成有效截图：$screenshot"
            }
            Write-Host "Captured $view $size -> $screenshot"
        }
    }
}
finally {
    if ($server -and -not $server.HasExited) {
        Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
        $server.WaitForExit(5000) | Out-Null
    }
    Remove-Item $readyFile -Force -ErrorAction SilentlyContinue
    Remove-Item $userDataRoot -Recurse -Force -ErrorAction SilentlyContinue
}
