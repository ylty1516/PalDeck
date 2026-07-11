# PalDeck v2.0.0 发布候选验证

- 日期：2026-07-11（UTC+8）
- 仓库：`F:\Grok_Workspace\01_Projects\palworld-mod-manager`
- 本轮基线：`bf192252686dc75666de180278ef773584ecff1b`
- 平台：Windows 11 `10.0.26200`、Python `3.13.5`、PyInstaller `6.21.0`
- 约束：未推送 GitHub，未复制到桌面；烟测只终止本次新增的 PalDeck PID，未操作 Palworld。

## 依赖锁验证

`requirements-lock.in` 汇总运行与测试依赖，并固定 `pyinstaller==6.21.0`。在 Windows Python 3.13 下执行：

```powershell
.venv-build\Scripts\python.exe -m piptools compile --generate-hashes --allow-unsafe --resolver=backtracking --output-file=requirements-lock.txt requirements-lock.in
```

退出码 `0`，生成的 `requirements-lock.txt` 固定运行、pytest、PyInstaller 及全部传递依赖，并包含 SHA-256。随后执行：

```powershell
$env:TMP="$PWD\.build-cache\tmp"
$env:TEMP="$PWD\.build-cache\temp"
$env:PIP_CACHE_DIR="$PWD\.build-cache\pip"
.venv-build\Scripts\python.exe -m pip install --dry-run --ignore-installed --require-hashes -r requirements-lock.txt
```

退出码 `0`，输出 `Would install` 全部锁定包；其中 PyInstaller 为 `6.21.0`、pytest 为 `9.1.1`。日志：被忽略的 `.tmp/lock-dry-run.log`。

## 自动测试与重新构建

预构建组合命令：

```text
.venv-build/Scripts/python.exe -m pytest -q
node --check frontend/*.js（逐文件）
.venv-build/Scripts/python.exe -m compileall -q backend launcher.py scripts
git diff --check
```

退出码 `0`：`309 passed, 3 skipped in 40.47s`；5 个 JavaScript、compileall 与 diff check 均通过。新增 3 个测试覆盖 F 盘报告门控、真实 HTTP cookie 自检成功报告及失败 error 报告。

重新执行正式构建：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build_portable.ps1
```

最终退出码 `0`。脚本从带哈希的 `requirements-lock.txt` 安装，随后得到 `309 passed, 3 skipped in 35.60s`，Node/compileall/diff check 通过；PyInstaller `6.21.0` 输出 `Build complete!`。`TMP`、`TEMP`、`PIP_CACHE_DIR`、`PYTHONPYCACHEPREFIX`、`PYINSTALLER_CONFIG_DIR` 均在 F 盘项目 `.build-cache` 内。最终完整日志：`.tmp/build-portable-final.log`。

## Nexus 真实在线验证

使用新的 F 盘缓存并强制 live：

```text
NexusCatalog(...).popular(force=True,count=3)
NexusCatalog(...).get(16,force=True)
```

退出码 `0`，两者均为 `source=live`、`stale=false`：

- popular：3 项，ID `16, 336, 577`，名称 `MapUnlocker`、`Pal Analyzer`、`Mod Config Menu (UI)`。
- ID 16：1 项，`MapUnlocker`，作者 `W1ns`，版本 `1.0.0`。

日志：`.tmp/nexus-live-fix.log`。

## 打包 EXE 真实端到端烟测

执行正式脚本：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke_portable.ps1
```

退出码 `0`。脚本将 ZIP 解压到 F 盘 `.tmp/portable-smoke/extracted`，使用全新的 `.tmp/portable-smoke/fresh-data`，并将运行时 TMP/TEMP 与 `PALDECK_SMOKE_REPORT` 全部指向 F 盘。EXE 正常打开 WebView；证据：

- 启动 PID `32532`，运行 PID `32532, 23944`。
- 窗口句柄 `4130592`。
- 本机监听 `127.0.0.1:7548`。
- 内部报告 `frozen=true`、总计 10 项且全部 `pass=true`。
- 真实 HTTP cookie session 验证：health 为 `up/v2.0.0`；fresh data 为 `configured=false,path=null`；GET appearance 成功。
- 三主题 `aurora-glass`、`ivory-sakura`、`starlit-night` 均 POST 后逐次 GET 确认。
- petals `high`、`off` 均 POST 后 GET 确认。
- 默认背景响应 `200 image/webp`，`191014` bytes。
- index 包含 `view-mods`、`view-import`、`view-nexus`、`view-settings` 与 `petalCanvas`。
- 验证后只终止启动前不存在的 PalDeck PID，输出 `smoke_processes_terminated=true`；无 PalDeck 残留。

结构化报告：被忽略的 `.tmp/portable-smoke/report.json`；最终完整日志：`.tmp/smoke-portable-final.log`。普通启动不设置 `PALDECK_SMOKE_REPORT`，该流程不启用；非 F 盘 `.json` 报告路径也不会启用。

## 当前产物与完整性

| 产物 | 字节数 | SHA-256 |
| --- | ---: | --- |
| `F:\Grok_Workspace\01_Projects\palworld-mod-manager\dist\PalDeck-portable\PalDeck.exe` | 23,309,207 | `97579cd9737db61d7ee304a4f8a36ebb1dbda1d63d2946bbc42ac6e8f2b3d784` |
| `F:\Grok_Workspace\01_Projects\palworld-mod-manager\dist\PalDeck-v2.0.0-windows-portable.zip` | 23,106,899 | `46673faf477ab9f3395281c83f95de80d8c735493a3cba67f6f75953a3b7817f` |
| `F:\Grok_Workspace\01_Projects\palworld-mod-manager\dist\PalDeck-v2.0.0-windows-portable.zip.sha256` | 校验文件 | 与重新计算值一致 |

ZIP 条目仍仅为：

```text
PalDeck-portable\PalDeck.exe
PalDeck-portable\README.txt
```

C 盘与进程检查再次退出 `0`：

```text
c_drive_project_intermediates_created_by_build=none
paldeck_processes=none
```

## 本轮执行中的失败与处置

1. 首次从 `.tmp/requirements-lock.in` 生成锁时，内部 `-r requirements.txt` 被按 `.tmp` 相对解析，返回 exit `1`；改为提交仓库根的 `requirements-lock.in` 后生成成功。
2. 初次生成锁未使用 `--allow-unsafe`，pip-tools 警告 setuptools 未锁；使用 `--allow-unsafe` 重生成后固定 `setuptools==83.0.0` 并附哈希，dry-run 验证成功。
3. GNU `sha256sum -c` 首次发现 PowerShell `Set-Content` 生成的 CRLF 会被 Bash 工具视为文件名字符；构建脚本改用明确 LF 写校验文件并重新完整构建。随后从仓库根直接校验仍因校验文件使用 ZIP 基名而失败；切换到 `dist` 目录执行后输出 `PalDeck-v2.0.0-windows-portable.zip: OK`。

本轮最终测试、构建、Nexus live、完整 EXE 烟测、哈希与清理检查均使用上述新鲜结果；未执行桌面复制、推送或 Release 上传。
