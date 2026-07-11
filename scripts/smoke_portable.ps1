[CmdletBinding()]
param(
    [string]$ZipPath = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $repoRoot.StartsWith("F:\", [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Portable smoke test must run from an F-drive repository."
}
Set-Location $repoRoot
if (-not $ZipPath) {
    $ZipPath = Join-Path $repoRoot "dist\PalDeck-v2.0.0-windows-portable.zip"
}
$ZipPath = (Resolve-Path $ZipPath).Path

$smokeRoot = Join-Path $repoRoot ".tmp\portable-smoke"
$extractRoot = Join-Path $smokeRoot "extracted"
$freshData = Join-Path $smokeRoot "fresh-data"
$runtimeTemp = Join-Path $smokeRoot "runtime-temp"
$reportPath = Join-Path $smokeRoot "report.json"
if (Test-Path $smokeRoot) { Remove-Item $smokeRoot -Recurse -Force }
New-Item -ItemType Directory -Path $extractRoot, $freshData, $runtimeTemp -Force | Out-Null

$env:TMP = $runtimeTemp
$env:TEMP = $runtimeTemp
$env:PALMOD_DATA_DIR = $freshData
$env:PALDECK_SMOKE_REPORT = $reportPath
Remove-Item Env:PALMOD_GAME_PATH -ErrorAction SilentlyContinue

Expand-Archive -Path $ZipPath -DestinationPath $extractRoot -Force
$exePath = Join-Path $extractRoot "PalDeck-portable\PalDeck.exe"
if (-not (Test-Path $exePath -PathType Leaf)) { throw "ZIP does not contain PalDeck.exe" }

$before = @(Get-Process -Name "PalDeck" -ErrorAction SilentlyContinue | ForEach-Object Id)
$started = $null
$evidence = $null
try {
    $started = Start-Process -FilePath $exePath -WorkingDirectory (Split-Path $exePath) -PassThru
    $deadline = (Get-Date).AddSeconds(90)
    while (-not (Test-Path $reportPath -PathType Leaf) -and (Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 250
    }
    if (-not (Test-Path $reportPath -PathType Leaf)) {
        throw "Timed out waiting for PALDECK_SMOKE_REPORT"
    }

    $report = Get-Content $reportPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($report.ok -ne $true) { throw "Packaged self-check failed: $($report.error)" }
    $failedItems = @($report.items | Where-Object { $_.pass -ne $true })
    if ($failedItems.Count -ne 0) { throw "Self-check report contains failed items" }
    $expectedItems = @(
        "index_four_views_and_petal_canvas", "health", "fresh_data_no_game_path", "appearance_get",
        "theme_aurora-glass", "theme_ivory-sakura", "theme_starlit-night",
        "petals_high", "petals_off", "default_background_webp"
    )
    $actualItems = @($report.items | ForEach-Object name)
    foreach ($name in $expectedItems) {
        if ($actualItems -notcontains $name) { throw "Self-check report missing item: $name" }
    }

    $newProcesses = @(Get-Process -Name "PalDeck" -ErrorAction SilentlyContinue |
        Where-Object { $before -notcontains $_.Id })
    while ((@($newProcesses | Where-Object MainWindowHandle -ne 0).Count -eq 0) -and (Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 250
        $newProcesses = @(Get-Process -Name "PalDeck" -ErrorAction SilentlyContinue |
            Where-Object { $before -notcontains $_.Id })
    }
    if ($newProcesses.Count -eq 0) { throw "PalDeck did not remain alive" }
    $windowProcesses = @($newProcesses | Where-Object MainWindowHandle -ne 0)
    if ($windowProcesses.Count -eq 0) { throw "PalDeck WebView window handle was not observed" }

    $uri = [Uri]$report.base_url
    $newIds = @($newProcesses | ForEach-Object Id)
    $listeners = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
        Where-Object { $newIds -contains $_.OwningProcess -and $_.LocalAddress -eq "127.0.0.1" -and $_.LocalPort -eq $uri.Port })
    if ($listeners.Count -eq 0) { throw "PalDeck loopback listener was not observed" }

    $evidence = [ordered]@{
        ok = $true
        zip_path = $ZipPath
        exe_path = $exePath
        started_pid = $started.Id
        alive_pids = @($newProcesses | ForEach-Object Id)
        window_handles = @($windowProcesses | ForEach-Object MainWindowHandle)
        listener = "127.0.0.1:$($uri.Port)"
        report_path = $reportPath
        report = $report
    }
    $evidence | ConvertTo-Json -Depth 8
}
finally {
    $current = @(Get-Process -Name "PalDeck" -ErrorAction SilentlyContinue |
        Where-Object { $before -notcontains $_.Id })
    if ($current.Count -ne 0) {
        $current | Stop-Process -Force
        Start-Sleep -Seconds 3
    }
    $remaining = @(Get-Process -Name "PalDeck" -ErrorAction SilentlyContinue |
        Where-Object { $before -notcontains $_.Id })
    if ($remaining.Count -ne 0) {
        throw "Smoke-created PalDeck processes remain: $($remaining.Id -join ',')"
    }
    Write-Host "smoke_processes_terminated=true"
}
