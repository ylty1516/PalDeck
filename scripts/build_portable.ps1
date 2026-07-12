[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $repoRoot.StartsWith("F:\", [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "便携版构建必须在 F: 盘项目目录内运行，当前目录：$repoRoot"
}
Set-Location $repoRoot

$packagedSourcePaths = @(
    "backend/",
    "frontend/",
    "launcher.py",
    "assets/",
    "bundled_mods/",
    "third_party/",
    "package.json",
    "requirements-lock*",
    "scripts/build_portable.ps1",
    "scripts/create_portable_zip.py",
    "build_exe.bat"
)
$dirtyPackagedSources = @(& git status --porcelain=v1 -- @packagedSourcePaths)
if ($LASTEXITCODE -ne 0) {
    throw "无法检查 packaged source paths 的 Git 状态"
}
if ($dirtyPackagedSources.Count -ne 0) {
    throw "packaged source paths 存在未提交修改，拒绝构建：`n$($dirtyPackagedSources -join "`n")"
}
$sourceCommit = (& git log -1 --format=%H -- @packagedSourcePaths).Trim()
if ($LASTEXITCODE -ne 0 -or $sourceCommit -notmatch "^[0-9a-f]{40}$") {
    throw "无法读取 packaged source commit"
}
$sourceEpoch = (& git show -s --format=%ct $sourceCommit).Trim()
if ($LASTEXITCODE -ne 0 -or $sourceEpoch -notmatch "^\d+$") {
    throw "无法从 packaged source commit 读取 SOURCE_DATE_EPOCH"
}
if (Test-Path "Env:PALMOD_VERSION") {
    throw "检测到 PALMOD_VERSION；发布构建拒绝环境变量覆盖版本"
}

$cacheRoot = Join-Path $repoRoot ".build-cache"
$env:TMP = Join-Path $cacheRoot "tmp"
$env:TEMP = Join-Path $cacheRoot "temp"
$env:PIP_CACHE_DIR = Join-Path $cacheRoot "pip"
$env:PYTHONPYCACHEPREFIX = Join-Path $cacheRoot "pycache"
$env:PYINSTALLER_CONFIG_DIR = Join-Path $cacheRoot "pyinstaller-config"
$env:PYTHONHASHSEED = "0"
$env:SOURCE_DATE_EPOCH = $sourceEpoch
Write-Host "SOURCE_COMMIT: $sourceCommit"
Write-Host "SOURCE_DATE_EPOCH: $($env:SOURCE_DATE_EPOCH)"
@($cacheRoot, $env:TMP, $env:TEMP, $env:PIP_CACHE_DIR, $env:PYTHONPYCACHEPREFIX, $env:PYINSTALLER_CONFIG_DIR) |
    ForEach-Object { New-Item -ItemType Directory -Path $_ -Force | Out-Null }

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $false)][string[]]$Arguments = @()
    )
    Write-Host "`n==> $Label"
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Label 失败（exit $LASTEXITCODE）"
    }
}

$venvDir = Join-Path $repoRoot ".venv-build"
$venvPython = Join-Path $venvDir "Scripts\python.exe"
$workDir = Join-Path $cacheRoot "pyinstaller-work"
$specDir = Join-Path $cacheRoot "spec"
$legacyBuildDir = Join-Path $repoRoot "build"
@($venvDir, $workDir, $specDir, $legacyBuildDir, $env:PYINSTALLER_CONFIG_DIR) | ForEach-Object {
    if (Test-Path $_) { Remove-Item $_ -Recurse -Force }
}

Invoke-Checked "确认 Python 3.13" "py" @(
    "-3.13", "-c", "import sys; assert sys.version_info[:2] == (3, 13), sys.version"
)
Invoke-Checked "重建隔离构建环境" "py" @("-3.13", "-m", "venv", $venvDir)
Invoke-Checked "确认构建环境 Python 3.13" $venvPython @(
    "-c", "import sys; assert sys.version_info[:2] == (3, 13), sys.version"
)

Invoke-Checked "安装锁定的运行、测试与打包依赖" $venvPython @(
    "-m", "pip", "install", "--disable-pip-version-check", "--require-hashes",
    "-r", (Join-Path $repoRoot "requirements-lock.txt")
)
$version = (& $venvPython -c "from backend.version import APP_VERSION; print(APP_VERSION)").Trim()
if ($LASTEXITCODE -ne 0 -or $version -notmatch "^\d+\.\d+\.\d+$") {
    throw "无法从 backend/version.py 读取有效 APP_VERSION"
}
Invoke-Checked "运行完整 pytest" $venvPython @("-m", "pytest", "-q")

$javascriptFiles = Get-ChildItem (Join-Path $repoRoot "frontend") -Filter "*.js" -File -Recurse | Sort-Object FullName
if ($javascriptFiles.Count -eq 0) {
    throw "frontend 下未找到 JavaScript 文件"
}
foreach ($file in $javascriptFiles) {
    Invoke-Checked "Node 语法检查：$($file.Name)" "node" @("--check", $file.FullName)
}
Invoke-Checked "运行 Node 测试" "npm" @("test")

Invoke-Checked "Python compileall" $venvPython @(
    "-m", "compileall", "-q",
    (Join-Path $repoRoot "backend"),
    (Join-Path $repoRoot "launcher.py"),
    (Join-Path $repoRoot "scripts")
)
Invoke-Checked "Git 空白错误检查" "git" @("diff", "--check")

$distDir = Join-Path $repoRoot "dist"
$portableDir = Join-Path $distDir "PalDeck-portable"
$zipPath = Join-Path $distDir "PalDeck-v$version-windows-portable.zip"
$shaPath = "$zipPath.sha256"
@($portableDir, $workDir, $specDir) | ForEach-Object {
    if (Test-Path $_) { Remove-Item $_ -Recurse -Force }
}
@($zipPath, $shaPath, (Join-Path $distDir "PalDeck.exe")) | ForEach-Object {
    if (Test-Path $_) { Remove-Item $_ -Force }
}
New-Item -ItemType Directory -Path $distDir -Force | Out-Null
New-Item -ItemType Directory -Path $portableDir -Force | Out-Null
New-Item -ItemType Directory -Path $workDir -Force | Out-Null
New-Item -ItemType Directory -Path $specDir -Force | Out-Null

$separator = [System.IO.Path]::PathSeparator
Invoke-Checked "PyInstaller 单文件窗口版构建" $venvPython @(
    "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--windowed",
    "--name", "PalDeck",
    "--icon", (Join-Path $repoRoot "assets\app.ico"),
    "--add-data", ((Join-Path $repoRoot "frontend") + $separator + "frontend"),
    "--add-data", ((Join-Path $repoRoot "assets") + $separator + "assets"),
    "--add-data", ((Join-Path $repoRoot "bundled_mods") + $separator + "bundled_mods"),
    "--add-data", ((Join-Path $repoRoot "third_party") + $separator + "third_party"),
    "--hidden-import", "webview",
    "--hidden-import", "webview.platforms.edgechromium",
    "--hidden-import", "flask",
    "--hidden-import", "PIL",
    "--hidden-import", "PIL.Image",
    "--hidden-import", "backend",
    "--hidden-import", "backend.app",
    "--collect-all", "webview",
    "--collect-all", "flask",
    "--distpath", $distDir,
    "--workpath", $workDir,
    "--specpath", $specDir,
    (Join-Path $repoRoot "launcher.py")
)

$builtExe = Join-Path $distDir "PalDeck.exe"
if (-not (Test-Path $builtExe -PathType Leaf)) {
    throw "PyInstaller 未生成 $builtExe"
}
Move-Item $builtExe (Join-Path $portableDir "PalDeck.exe") -Force

$portableReadme = @"
PalDeck v$version - Windows 便携版

1. 解压整个 PalDeck-portable 文件夹。
2. 双击 PalDeck.exe；运行数据保存在程序旁的 data 文件夹。
3. 支持 Windows 10/11 的 Steam 客户端版 Palworld。
4. 修改或删除 Mod 前请备份存档和游戏文件；请勿在游戏运行时操作。
5. Nexus 功能仅匿名只读浏览，不提供登录、下载或自动安装。
6. 只管理 Steam 客户端已订阅并下载的 Workshop 模组启停；不提供 Workshop 订阅、取消订阅或删除。
7. Workshop UE4SS 与手动或内置 UE4SS 互斥，请先禁用或移除另一来源再切换。
8. 内置 Palworld 专用 UE4SS 来自 Okaetsu/RE-UE4SS（MIT）；许可证与致谢随程序分发，并可在“致谢”页查看。
9. 不支持 Xbox/Microsoft Store 或专用服务器 Mod 管理。

项目说明见：https://github.com/ylty1516/PalDeck
Source commit: $sourceCommit
"@
[System.IO.File]::WriteAllText(
    (Join-Path $portableDir "README.txt"), $portableReadme + "`n", (New-Object System.Text.UTF8Encoding($false))
)

Invoke-Checked "创建确定性便携 ZIP" $venvPython @(
    (Join-Path $repoRoot "scripts\create_portable_zip.py"),
    $portableDir,
    $zipPath,
    "--epoch", $env:SOURCE_DATE_EPOCH
)
$hash = (Get-FileHash -Path $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
$hashLine = "$hash  $([System.IO.Path]::GetFileName($zipPath))"
[System.IO.File]::WriteAllText($shaPath, $hashLine + "`n", [System.Text.Encoding]::ASCII)

$exeInfo = Get-Item (Join-Path $portableDir "PalDeck.exe")
$zipInfo = Get-Item $zipPath
Write-Host "`n构建完成（未复制到桌面）："
Write-Host "EXE: $($exeInfo.FullName) ($($exeInfo.Length) bytes)"
Write-Host "ZIP: $($zipInfo.FullName) ($($zipInfo.Length) bytes)"
Write-Host "SHA256: $hash"
Write-Host "校验文件: $shaPath"
