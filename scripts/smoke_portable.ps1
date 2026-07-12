[CmdletBinding()]
param(
    [string]$ZipPath = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Get-ProcessTreeIds {
    param([Parameter(Mandatory = $true)][int]$RootId)
    $rows = @(Get-CimInstance Win32_Process -ErrorAction Stop)
    $ids = New-Object 'System.Collections.Generic.HashSet[int]'
    $ids.Add($RootId) | Out-Null
    do {
        $changed = $false
        foreach ($row in $rows) {
            if ($ids.Contains([int]$row.ParentProcessId) -and $ids.Add([int]$row.ProcessId)) {
                $changed = $true
            }
        }
    } while ($changed)
    return @($ids)
}

function Add-ProcessTreeIds {
    param(
        [Parameter(Mandatory = $true)][int]$RootId,
        [Parameter(Mandatory = $true)]$OwnedIds
    )
    foreach ($processId in @(Get-ProcessTreeIds -RootId $RootId)) {
        $OwnedIds.Add([int]$processId) | Out-Null
    }
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $repoRoot.StartsWith("F:\", [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Portable smoke test must run from an F-drive repository."
}
Set-Location $repoRoot

$smokeRoot = Join-Path $repoRoot ".tmp\portable-smoke"
$extractRoot = Join-Path $smokeRoot "extracted"
$freshData = Join-Path $smokeRoot "fresh-data"
$runtimeTemp = Join-Path $smokeRoot "runtime-temp"
$reportPath = Join-Path $smokeRoot "report.json"
if (Test-Path $smokeRoot) { Remove-Item $smokeRoot -Recurse -Force }
New-Item -ItemType Directory -Path $extractRoot, $freshData, $runtimeTemp -Force | Out-Null

$env:TMP = $runtimeTemp
$env:TEMP = $runtimeTemp
$env:PYTHONPYCACHEPREFIX = Join-Path $runtimeTemp "pycache"
$env:PALMOD_DATA_DIR = $freshData
Remove-Item Env:PALMOD_GAME_PATH -ErrorAction SilentlyContinue

if (-not $ZipPath) {
    $version = (& py -3.13 -c "from backend.version import APP_VERSION; print(APP_VERSION)").Trim()
    if ($LASTEXITCODE -ne 0 -or -not $version) { throw "Cannot read APP_VERSION with Python 3.13" }
    $ZipPath = Join-Path $repoRoot "dist\PalDeck-v$version-windows-portable.zip"
}
$ZipPath = (Resolve-Path $ZipPath).Path

$handshake = [guid]::NewGuid().ToString("N")
$markerPath = Join-Path $freshData ".paldeck-smoke-$handshake"
[System.IO.File]::WriteAllText($markerPath, $handshake, [System.Text.Encoding]::ASCII)
$env:PALDECK_SMOKE_REPORT = $reportPath
$env:PALDECK_SMOKE_HANDSHAKE = $handshake

Expand-Archive -Path $ZipPath -DestinationPath $extractRoot -Force
$exePath = Join-Path $extractRoot "PalDeck-portable\PalDeck.exe"
if (-not (Test-Path $exePath -PathType Leaf)) { throw "ZIP does not contain PalDeck.exe" }

$started = $null
$ownedIds = New-Object 'System.Collections.Generic.HashSet[int]'
try {
    $started = Start-Process -FilePath $exePath -WorkingDirectory (Split-Path $exePath) -PassThru
    $ownedIds.Add([int]$started.Id) | Out-Null
    $deadline = (Get-Date).AddSeconds(90)
    while (-not (Test-Path $reportPath -PathType Leaf) -and (Get-Date) -lt $deadline) {
        Add-ProcessTreeIds -RootId $started.Id -OwnedIds $ownedIds
        Start-Sleep -Milliseconds 250
    }
    if (-not (Test-Path $reportPath -PathType Leaf)) {
        throw "Timed out waiting for PALDECK_SMOKE_REPORT"
    }
    while ((Test-Path $markerPath) -and (Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 50
    }
    if (Test-Path $markerPath) { throw "Smoke handshake marker was not removed" }

    Add-ProcessTreeIds -RootId $started.Id -OwnedIds $ownedIds
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
    if (-not $ownedIds.Contains([int]$report.pid)) {
        throw "Self-check PID is not a descendant of the started process"
    }

    $treeProcesses = @(foreach ($processId in $ownedIds) {
        Get-Process -Id $processId -ErrorAction SilentlyContinue
    })
    while ((@($treeProcesses | Where-Object MainWindowHandle -ne 0).Count -eq 0) -and (Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 250
        Add-ProcessTreeIds -RootId $started.Id -OwnedIds $ownedIds
        $treeProcesses = @(foreach ($processId in $ownedIds) {
            Get-Process -Id $processId -ErrorAction SilentlyContinue
        })
    }
    if ($treeProcesses.Count -eq 0) { throw "PalDeck process tree did not remain alive" }
    $windowProcesses = @($treeProcesses | Where-Object MainWindowHandle -ne 0)
    if ($windowProcesses.Count -eq 0) { throw "PalDeck WebView window handle was not observed" }

    $uri = [Uri]$report.base_url
    $listeners = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
        Where-Object { $ownedIds.Contains([int]$_.OwningProcess) -and $_.LocalAddress -eq "127.0.0.1" -and $_.LocalPort -eq $uri.Port })
    if ($listeners.Count -eq 0) { throw "PalDeck loopback listener was not observed" }

    [ordered]@{
        ok = $true
        zip_path = $ZipPath
        exe_path = $exePath
        started_pid = $started.Id
        process_tree_pids = @($ownedIds)
        window_handles = @($windowProcesses | ForEach-Object MainWindowHandle)
        listener = "127.0.0.1:$($uri.Port)"
        report_path = $reportPath
        marker_removed = -not (Test-Path $markerPath)
        report = $report
    } | ConvertTo-Json -Depth 8
}
finally {
    if ($started -ne $null) {
        Add-ProcessTreeIds -RootId $started.Id -OwnedIds $ownedIds
        $descendants = @($ownedIds | Where-Object { $_ -ne $started.Id })
        foreach ($processId in $descendants) {
            Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
        }
        Stop-Process -Id $started.Id -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 3
        $remaining = @(foreach ($processId in $ownedIds) {
            Get-Process -Id $processId -ErrorAction SilentlyContinue
        })
        if ($remaining.Count -ne 0) {
            throw "Smoke process tree remains: $($remaining.Id -join ',')"
        }
    }
    Remove-Item $markerPath -Force -ErrorAction SilentlyContinue
    Write-Host "smoke_process_tree_terminated=true"
}
