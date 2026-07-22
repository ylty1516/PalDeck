# PalDeck v2.0.0 最终源码对应发布验证

- 验证日期：2026-07-12（UTC+8）
- 仓库：`F:\Grok_Workspace\01_Projects\palworld-mod-manager`
- packaged source commit：`54d6a263e742a9da0791a006df00ae0c504b8f45`
- packaged source 代码提交：`fix: 绑定便携产物与源码提交`
- `SOURCE_DATE_EPOCH`：`1783835567`（取自上述 packaged source commit）
- 平台：Windows 11 `10.0.26200`、Python `3.13.5`、PyInstaller `6.21.0`
- 约束：未推送、未复制到桌面。

## 源码与产物绑定

`scripts/build_portable.ps1` 将以下路径定义为 packaged source paths：

```text
backend/
frontend/
launcher.py
assets/
bundled_mods/
requirements-lock*
scripts/build_portable.ps1
scripts/create_portable_zip.py
build_exe.bat
```

构建开始时使用 `git status --porcelain=v1 -- <paths>` 拒绝这些路径中的 tracked、staged 或 untracked dirty 状态；使用 `git log -1 --format=%H -- <paths>` 确定 packaged source commit，再从该提交读取 commit timestamp 作为 `SOURCE_DATE_EPOCH`。构建拒绝存在 `PALMOD_VERSION` 的环境；正式烟测显式清除该变量并从 `backend.version.APP_VERSION` 读取版本。

正式构建日志均输出：

```text
SOURCE_COMMIT: 54d6a263e742a9da0791a006df00ae0c504b8f45
SOURCE_DATE_EPOCH: 1783835567
```

ZIP 内 `PalDeck-portable/README.txt` 的绑定行经 Python 精确断言为：

```text
Source commit: 54d6a263e742a9da0791a006df00ae0c504b8f45
```

## 测试与全新构建

新增 `tests/test_release_script_contract.py`，覆盖 packaged source 路径、dirty 拒绝、source commit/epoch 取值、README/日志绑定及 smoke 环境版本覆盖清理。定向测试 `2 passed`；每次正式构建均删除并重建 `.venv-build`、PyInstaller work/spec/config，并执行：

```text
316 passed, 3 skipped
5 个 frontend JavaScript 文件 node --check 通过
Python compileall 通过
git diff --check 通过
```

连续两次从 clean `54d6a263e742a9da0791a006df00ae0c504b8f45` 全新构建，EXE 与 ZIP 的 SHA-256 均精确一致：

```text
reproducible_hashes_match=true
```

日志及哈希快照：

- `.tmp/build-source-54d6a26.log`
- `.tmp/build-source-54d6a26-repro.log`
- `.tmp/source-54d6a26-build-1.sha256`
- `.tmp/source-54d6a26-build-2.sha256`

## 正式运行与在线验证

`scripts/smoke_portable.ps1` 对正式 ZIP exit `0`：真实 frozen EXE 建立 WebView 窗口和 `127.0.0.1` listener，fresh-data handshake marker 被移除，10 项 HTTP 自检全部 `pass=true`，health 报告 `version=2.0.0`、`frozen=true`；脚本最终输出 `smoke_process_tree_terminated=true`，无 PalDeck 残留。日志：`.tmp/smoke-source-54d6a26.log`。

Nexus 真实在线强制请求 `popular(force=True,count=3)` 与 `get(16,force=True)` 均为 `source=live`、`stale=false`。popular ID 为 `16, 336, 577`，ID 16 为 `MapUnlocker`。日志：`.tmp/nexus-live-source-54d6a26.log`。

`sha256sum -c PalDeck-v2.0.0-windows-portable.zip.sha256` 输出 `OK`。C 盘检查输出：

```text
c_drive_project_intermediates_created_by_hardened_build=none
paldeck_processes=none
```

日志：`.tmp/check-clean-source-54d6a26.log`。

## 最终产物

| 产物 | 字节数 | SHA-256 |
| --- | ---: | --- |
| `dist/PalDeck-portable/PalDeck.exe` | 23,314,105 | `d5f3d15701919755f05256877358c22a273fc26f8ecdb972e76ed3b04f406ead` |
| `dist/PalDeck-v2.0.0-windows-portable.zip` | 23,111,265 | `31688ff09017d190cafaa92a311e2282804bf4e0e095ec7d585f7f70bf7ff0e3` |

sidecar 内容精确绑定上述 ZIP 文件名及 SHA-256。

## 最终 HEAD 与 packaged source commit 的关系

最终发布 HEAD 可以是 packaged source commit 之后的纯验证文档 attestation commit；产物仍绑定 packaged source commit，而不是把纯文档提交伪装成产物源码。提交本文件后执行：

```powershell
git diff --name-only 54d6a263e742a9da0791a006df00ae0c504b8f45..HEAD
```

唯一输出应且实际验证为：

```text
docs/verification/2026-07-11-paldeck-release.md
```

由于 `docs/verification/` 不属于 packaged source paths，最终 HEAD 上再次求值 packaged source commit 仍为 `54d6a263e742a9da0791a006df00ae0c504b8f45`，epoch 仍为 `1783835567`。最终文档提交没有修改代码或产物输入，因此无需、也不应以文档提交替换已验证产物；当前产物是最后一次源码构建结果。
