# PalDeck v2.0.0 发布候选最终验证

- 最终质量验证日期：2026-07-12（UTC+8）
- 仓库：`F:\Grok_Workspace\01_Projects\palworld-mod-manager`
- 本轮基线：`df4cd0e8870b126a003f51f98f5d393d6c3b0057`
- 平台：Windows 11 `10.0.26200`、Python `3.13.5`、PyInstaller `6.21.0`
- 约束：未推送 GitHub、未复制桌面；烟测仅清理从本次 `Start-Process` PID 递归得到的 Win32 子孙树，未按进程名或 PID 差集终止进程。

## TDD 与自动验证

先新增 5 个安全/契约测试并运行红灯，初次因尚无 `smoke_context` 返回 ImportError（exit `4`）。实现后定向测试为 `7 passed`，随后两次全新构建分别运行完整测试：

```text
build 1: 314 passed, 3 skipped in 33.55s
build 2: 314 passed, 3 skipped in 30.61s
```

新增的 5 个测试覆盖：

1. `/api/update/apply` 拒绝任意 URL 参数且不调用 updater。
2. Release 资产缺少 checksum sidecar 时安全失败。
3. SHA-256 不匹配时安全失败并删除下载文件。
4. 正确哈希和精确资产名时接受，并验证 ZIP/sidecar 配对。
5. frozen + F 盘报告 + 随机 handshake + fresh-data marker 门控，以及烟测脚本只能使用 Win32 `ParentProcessId` 进程树。

每次构建还逐文件通过 5 个 `node --check`、Python `compileall` 与 `git diff --check`。

## 自更新信任链

实现和测试确认：

- latest release API 固定为 `https://api.github.com/repos/ylty1516/palworld-mod-manager/releases/latest`，不再受环境变量仓库覆盖影响。
- `/api/update/apply` 不接受 URL 或其他参数；前端只发送空对象。
- 只接受受信任仓库的 HTTPS GitHub Release URL，并限制 GitHub 官方重定向主机。
- `check_for_update()` 同时返回主资产及精确同名 `.sha256` sidecar。
- sidecar 必须严格为 `64hex + 两个空格 + 精确资产名`。
- 先下载 sidecar，再下载资产，使用 `hmac.compare_digest` 比较实际 SHA-256；缺失、格式错误或哈希不匹配均删除暂存下载并停止。
- README 明确要求发布时同时上传 ZIP 与同名 `.sha256`。

## 隔离和可复现构建

连续两次执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build_portable.ps1
```

两次均 exit `0`。每次实际删除并重建 `.venv-build`、`build`、PyInstaller work/spec/config，强制 `py -3.13` 并断言 `sys.version_info[:2] == (3, 13)`，再以 `--require-hashes` 安装锁文件。`TMP`、`TEMP`、`PIP_CACHE_DIR`、`PYTHONPYCACHEPREFIX`、`PYINSTALLER_CONFIG_DIR` 全在 F 盘项目内；`SOURCE_DATE_EPOCH` 取自 `git log -1 --format=%ct`。

版本由构建环境导入 `backend.version.APP_VERSION`，构建和烟测脚本均未硬编码 `2.0.0`。ZIP 由 `scripts/create_portable_zip.py` 以固定时间、权限、排序和压缩参数生成，不再使用 `Compress-Archive`。

两次全新构建的 EXE 与 ZIP 哈希完全相同：

```text
EXE 34cab58aa897aa736c8efe2e319f2be095795081027197a12a2f5f83d1dd5f48
ZIP 313169345ce02f0e6e504fce53918504bb6020f24280447909bbe1c93fe62b09
reproducible_hashes_match=true
```

日志：`.tmp/build-hardened-1.log`、`.tmp/build-hardened-2.log`；哈希快照：`.tmp/repro-build-1.sha256`、`.tmp/repro-build-2.sha256`。

## 真实 EXE 烟测

执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke_portable.ps1
```

exit `0`。脚本从 `started_pid=20852` 出发，通过 `Win32_Process.ParentProcessId` 递归获得本次真实进程树 `20852, 17948, 13416, 5776`；未扫描 PalDeck 名称或使用启动前后 PID 差集。

证据：

- fresh data、TMP/TEMP、report 和一次性 marker 均位于 F 盘。
- 随机 32 hex handshake 与 `.paldeck-smoke-<handshake>` marker 验证成功，报告生成后 marker 已删除：`marker_removed=true`。
- frozen EXE 内部使用本次 session token 建立真实 HTTP cookie session，10 项报告全部 `pass=true`。
- health：`up`、`v2.0.0`、`frozen=true`；fresh data：`configured=false,path=null`。
- 三主题逐次 POST/GET 确认；petals `high/off` 分别 POST/GET 确认。
- 默认背景 `200 image/webp`、`191014` bytes；index 含四个 view 与 `petalCanvas`。
- WebView 窗口句柄 `4719458`，监听 `127.0.0.1:9570`。
- 最后仅按已收集的子孙树 PID 先子后根清理，输出 `smoke_process_tree_terminated=true`，无 PalDeck 残留。

日志：`.tmp/smoke-hardened.log`；结构化报告：`.tmp/portable-smoke/report.json`。

## Nexus 真实在线验证

以新的 F 盘缓存强制执行 `popular(force=True,count=3)` 与 `get(16,force=True)`，exit `0`：

- 两者均 `source=live`、`stale=false`。
- popular ID：`16, 336, 577`，名称：`MapUnlocker`、`Pal Analyzer`、`Mod Config Menu (UI)`。
- ID 16：1 项，名称 `MapUnlocker`。

日志：`.tmp/nexus-live-hardened.log`。

## 最终产物与完整性

| 产物 | 字节数 | SHA-256 |
| --- | ---: | --- |
| `F:\Grok_Workspace\01_Projects\palworld-mod-manager\dist\PalDeck-portable\PalDeck.exe` | 23,314,104 | `34cab58aa897aa736c8efe2e319f2be095795081027197a12a2f5f83d1dd5f48` |
| `F:\Grok_Workspace\01_Projects\palworld-mod-manager\dist\PalDeck-v2.0.0-windows-portable.zip` | 23,111,216 | `313169345ce02f0e6e504fce53918504bb6020f24280447909bbe1c93fe62b09` |

`cd dist && sha256sum -c PalDeck-v2.0.0-windows-portable.zip.sha256` 输出 `OK`。ZIP 条目固定排序为：

```text
PalDeck-portable/PalDeck.exe
PalDeck-portable/README.txt
```

使用 `.tmp/check-clean-hardened.ps1` 按本轮开始时间检查，exit `0`：

```text
c_drive_project_intermediates_created_by_hardened_build=none
paldeck_processes=none
```

系统 TEMP 中存在一个 2026-07-11 23:39 创建的旧 `paldeck-lock-dryrun.txt`，早于本轮 2026-07-12 构建，未删除也未计作本轮中间文件。

## 本轮失败与处置

1. TDD 红灯阶段缺少 `smoke_context`，测试收集按预期失败；实现门控后绿灯。
2. 一次从 Bash 调用的内联 PowerShell C 盘检查因 `$env` 被 Bash 展开而失败；按约束改用 `.tmp/check-clean-hardened.ps1`，得到上述成功证据。

最终没有复制桌面、推送 GitHub 或上传 Release。
