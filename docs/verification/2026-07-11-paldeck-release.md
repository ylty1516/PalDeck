# PalDeck v2.0.0 发布候选验证

- 日期：2026-07-11（UTC+8）
- 仓库：`F:\Grok_Workspace\01_Projects\palworld-mod-manager`
- 基线：`a0e975168e10427f25956e4aa4c4d1c93b90505d`
- 平台：Windows 11 `10.0.26200`、Python `3.13.5`、PyInstaller `6.21.0`
- 约束：未推送 GitHub，未复制到桌面；冒烟验证仅终止本次启动的 `PalDeck.exe`，未操作 Palworld 进程。

## 构建与自动验证

执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build_portable.ps1
```

最终退出码为 `0`。脚本固定仓库根目录，并将 `TMP`、`TEMP`、`PIP_CACHE_DIR`、`PYTHONPYCACHEPREFIX`、`PYINSTALLER_CONFIG_DIR` 分别指向项目内 `.build-cache` 的子目录；创建了项目内 `.venv-build`。依赖安装、完整验证与构建结果：

- `pip install -r requirements.txt -r requirements-dev.txt pyinstaller`：exit `0`。
- `.venv-build\Scripts\python.exe -m pytest -q`：exit `0`，`306 passed, 3 skipped in 23.55s`。
- `node --check`：`frontend/api.js`、`app.js`、`effects.js`、`interaction-policy.js`、`render.js` 各 exit `0`。
- `.venv-build\Scripts\python.exe -m compileall -q backend launcher.py scripts`：exit `0`。
- `git diff --check`：exit `0`；仅输出 README 的 Git 行尾转换 warning，无空白错误。
- PyInstaller `--onefile --windowed --name PalDeck`（包含 icon、frontend、assets、bundled_mods 及 webview/flask/PIL hidden imports）：exit `0`，日志为 `Build complete!`。

完整构建日志保存在被忽略的 `F:\Grok_Workspace\01_Projects\palworld-mod-manager\.tmp\build-portable.log`。

在全部源码与本文档写入后又执行了一次新鲜验证：

```text
.venv-build/Scripts/python.exe -m pytest -q
node --check frontend/*.js（逐文件）
.venv-build/Scripts/python.exe -m compileall -q backend launcher.py scripts
git diff --check
```

组合命令 exit `0`；最终一次 pytest 为 `306 passed, 3 skipped in 31.14s`，5 个 JavaScript 检查、compileall 与 diff check 均通过。日志位于被忽略的 `.tmp/final-validation.log`。

## Nexus 真实在线验证

执行（缓存位于 F 盘项目内 `.tmp`）：

```powershell
$env:PALMOD_LIVE_CACHE='F:/Grok_Workspace/01_Projects/palworld-mod-manager/.tmp/nexus-live-20260711'
.venv-build\Scripts\python.exe -c "... NexusCatalog(...).popular(force=True,count=3); NexusCatalog(...).get(16,force=True) ..."
```

退出码 `0`，两次结果均为 `source=live`、`stale=false`：

- popular：3 项；ID 为 `16, 336, 577`；名称为 `MapUnlocker`、`Pal Analyzer`、`Mod Config Menu (UI)`。
- ID 16：1 项；`MapUnlocker`，作者 `W1ns`，版本 `1.0.0`，Nexus URL `https://www.nexusmods.com/palworld/mods/16`。

原始 JSON 摘要保存在被忽略的 `.tmp/nexus-live.log`。

## 产物、ZIP 与 SHA-256

| 产物 | 字节数 | SHA-256 |
| --- | ---: | --- |
| `F:\Grok_Workspace\01_Projects\palworld-mod-manager\dist\PalDeck-portable\PalDeck.exe` | 23,304,078 | `4c1b167c8106b0c0f4a7a50b78de71ee9fad2d8a984e9591c41892188024b0b5` |
| `F:\Grok_Workspace\01_Projects\palworld-mod-manager\dist\PalDeck-v2.0.0-windows-portable.zip` | 23,100,667 | `54f3f4782f9c6e9d667c29bc548a43ddd7931c9f3469aa82e3d892f49eab0c98` |
| `F:\Grok_Workspace\01_Projects\palworld-mod-manager\dist\PalDeck-v2.0.0-windows-portable.zip.sha256` | 校验文件 | 内容中的哈希与重新计算值一致 |

ZIP 实际条目仅为：

```text
PalDeck-portable\PalDeck.exe
PalDeck-portable\README.txt
```

## F 盘解压启动冒烟

执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .tmp/smoke-release.ps1
```

退出码 `0`。脚本在 `F:\Grok_Workspace\01_Projects\palworld-mod-manager\.tmp\release-smoke` 解压，并将本次运行的 `TMP`/`TEMP` 指向 F 盘 `.tmp\smoke-runtime`。证据：

- ZIP 预期哈希与实际哈希均为 `54f3f4782f9c6e9d667c29bc548a43ddd7931c9f3469aa82e3d892f49eab0c98`。
- 解压后的 EXE 存在，大小 `23,304,078` 字节。
- 启动 PID `30136`；等待 20 秒后本次新增 PID `30136, 34240` 均存活。
- PID `34240` 的窗口句柄为 `3542310`，并监听 `127.0.0.1:10524`。
- 随后仅对启动前不存在的上述 PalDeck PID 执行 `Stop-Process -Force`；3 秒后无本次 PalDeck 进程残留，输出 `smoke_processes_terminated=true`。

原始证据保存在被忽略的 `.tmp/smoke-release.log`。

## C 盘中间文件与进程检查

执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .tmp/check-clean.ps1
```

按本次构建开始时间 `2026-07-11 22:50 +08:00` 检查系统 TEMP 的 `_MEI*` / `PalDeck*`、`%LOCALAPPDATA%\pyinstaller` 以及 C 盘根目录的项目名条目，退出码 `0`：

```text
c_drive_project_intermediates_created_by_build=none
paldeck_processes=none
```

系统 TEMP 中另有一个创建于 `20:47:50` 的既有 `_MEI285842`，早于本次构建，未删除也未计为本次中间文件。

## 执行中失败与处置

1. 首次直接运行 `py -3 -m pytest -q tests/test_self_updater.py` 返回 `No module named pytest`（exit `1`）；随后由构建脚本创建 `.venv-build`、安装依赖并完成全量测试。
2. 首次执行构建脚本因 Windows PowerShell 5.1 将无 BOM UTF-8 脚本按本地代码页解析而出现 ParserError（exit `1`）；将 `scripts/build_portable.ps1` 保存为 UTF-8 BOM 后重跑成功。
3. 一次内联 PowerShell 解析检查和一次内联 C 盘检查因 Bash 提前展开 `$` 变量而失败；改为执行 `.ps1` 文件后得到上述有效结果。
4. 初次 C 盘检查发现早于本次任务的 `_MEI285842` 并返回 exit `1`；核对其创建时间后，改为按本次构建时间窗口检查，确认本次未创建 C 盘项目中间文件。

未执行 GitHub 推送、Release 上传或桌面复制。
