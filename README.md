# PalDeck — 幻兽帕鲁 Mod 管理器

PalDeck 是面向 **Windows 10/11、Steam 客户端版 Palworld** 的便携桌面 Mod 管理器。程序使用本机 Flask 服务与 pywebview 窗口，配置、清单、背景和缓存保存在 `PalDeck.exe` 同目录的 `data` 文件夹。

## 下载与运行

从 [Releases](https://github.com/ylty1516/PalDeck/releases) 下载 `PalDeck-v2.3.2-windows-portable.zip`，完整解压后双击 `PalDeck-portable/PalDeck.exe`。建议安装 [Microsoft Edge WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/)；Windows 11 通常已自带。

应用内自更新只信任正式仓库 `ylty1516/PalDeck` 的 GitHub Release，并在 SHA-256 验证后于退出时替换当前程序；不接受客户端提供的下载 URL。更新资产必须同时提供同名 `.sha256`，可识别 `PalDeck.exe` / PalDeck 便携 ZIP，旧版 `PalMod.exe` 资源名称仍保持兼容。

## 支持范围与真实功能

- 自动检测 Steam 库中的 Palworld，也可手动选择经过验证的游戏目录。
- 导入 `.zip` 或 `.pak`，识别并安装普通 PAK、LogicMods、UE4SS Lua/C++ Mod 和 PalSchema 内容模组。
- 自动发现 `UE4SS/Mods/PalSchema/mods/<模组名>` 下的独立 PalSchema 内容模组，支持安全启停、取消管理、回收站删除与恢复。
- 对带根级 `config.json` 的受管 UE4SS Mod 进行保守数值检测；支持作者提供的 `palmod_config.json` 范围与说明，并在 Mod 条目中安全调整。PalDeck 不扫描或改写任意 Lua 代码中的数字。
- 直接导入 PAK 时处理同名 `.pak` / `.utoc` / `.ucas` 文件组。
- 通过受管理清单启用、禁用和删除手动 Mod，并检查磁盘文件状态。
- 首次读取会发现安装 PalDeck 前已有的 `~mods`、`LogicMods`、PalSchema 内容模组以及 classic/nested 两种 UE4SS 布局，并标记为“外部发现”；可取消管理并保留游戏文件，也可在设置中重新发现。
- 扫描 Steam 客户端已订阅并下载的 Workshop 项目，显示来源、作者、版本、依赖和安装类型，并管理启停状态。
- 提供由 [Okaetsu/RE-UE4SS](https://github.com/Okaetsu/RE-UE4SS) 维护的固定 Palworld 专用 UE4SS 内置包，也可检查固定上游 Release 或选择本地 ZIP；PalDeck 会记录所有权、审计完整性、保留用户配置修复，并安全卸载到回收站。
- 匿名、只读浏览和搜索 Nexus Mods，显示图片、作者、版本、统计与 Nexus ID；Nexus 成人内容会被彻底过滤，不会出现在目录、搜索、详情或缓存结果中。
- 本地受管 Mod 删除会进入 PalDeck 自有回收站并保留 30 天，支持恢复和手动彻底删除；恢复冲突、损坏记录和异常路径均 fail-closed。
- v2.3 延续响应式蓝白玻璃界面，提供外部来源标签、回收站对话框、UE4SS 生命周期状态和四档视觉回归基线。
- 三套主题：极光玻璃、象牙樱花、星夜；支持自定义背景、遮罩、模糊、位置、动态樱花密度及自然、水彩、简约三种樱花风格。
- 内置致谢页列出关键项目、直接依赖、来源与许可证。
- 绿色便携数据目录，不需要安装到系统目录。

## 明确不支持

- Xbox / Microsoft Store 版 Palworld。
- Palworld 专用服务器 Mod 管理。
- Steam Workshop 的订阅、取消订阅、删除或缓存修改；PalDeck 只管理 Steam 已下载项目的启停。
- Nexus 登录、下载或自动安装；Nexus 功能仅为匿名只读目录。

## Workshop 与 UE4SS 注意事项

Workshop UE4SS 与手动或内置 UE4SS **互斥**：启用 Workshop UE4SS 前需移除手动/内置版本；安装手动或内置版本前需先禁用 Workshop UE4SS。PalDeck 不会替用户订阅或删除 Workshop 内容，这些操作仍应在 Steam 客户端完成。

内置和本地 ZIP 安装会在用户实际的 Palworld `Win64` 目录创建短期事务暂存，因而支持任意 Steam Library 盘符，并保证发布、备份与回滚位于同一文件系统；操作结束后暂存目录会删除。

## 风险与备份

Mod 会修改游戏目录，第三方 Mod 也可能不兼容当前游戏版本。**安装、启停或删除 Mod 前，请备份 Palworld 存档和相关游戏文件。** 不要在 Palworld 运行时操作 Mod；遇到异常时先禁用 Mod，并使用 Steam 校验游戏文件。PalDeck 不能保证第三方 Mod 的安全性或兼容性。

## 从源码运行

需要 Python 3.10+、Node.js（用于前端语法检查）和 Microsoft Edge WebView2 Runtime：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements-lock.txt
.\.venv\Scripts\python.exe launcher.py
```

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
Get-ChildItem frontend -Filter *.js | ForEach-Object { node --check $_.FullName }
.\.venv\Scripts\python.exe -m compileall -q backend launcher.py scripts
```

## 构建 Windows 便携版

构建必须从 F 盘的项目工作区执行，并使用 Python 3.13。脚本固定仓库根目录，每次删除并重建 `.venv-build` 与 PyInstaller 工作目录，按 `requirements-lock.txt` 的固定版本和哈希安装依赖，先运行完整验证，再生成 one-file/windowed 产物。版本仅从 `backend/version.py` 读取，ZIP 条目按 Git commit 的 `SOURCE_DATE_EPOCH` 固定排序、时间与权限；这保证固定 Windows/Python/依赖环境下的可复现流程，但仍需连续构建比较产物哈希：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build_portable.ps1
# 或
.\build_exe.bat
```

输出：

- `dist/PalDeck-portable/PalDeck.exe`
- `dist/PalDeck-portable/README.txt`
- `dist/PalDeck-v2.3.2-windows-portable.zip`
- `dist/PalDeck-v2.3.2-windows-portable.zip.sha256`

发布 GitHub Release 时，必须同时上传便携 ZIP 与构建生成的同名 `.sha256`（例如 `PalDeck-v2.3.2-windows-portable.zip` 和 `PalDeck-v2.3.2-windows-portable.zip.sha256`）；缺少或不匹配时应用会安全拒绝更新。构建脚本不会复制文件到桌面。构建完成后可运行正式冒烟脚本；它使用 F 盘 fresh data/TEMP，启动真实 EXE 和 WebView，并通过内部 HTTP 会话验证五个视图、三套主题、三种樱花风格、内置 UE4SS 来源/摘要界面标记、Workshop 空状态及默认背景，全程不写入真实游戏目录：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke_portable.ps1
```

`PALDECK_SMOKE_REPORT` 与随机 `PALDECK_SMOKE_HANDSHAKE` 仅供该发布自检使用；流程还要求 frozen EXE、F 盘尚不存在的报告文件及 fresh data 中的一次性 marker。普通启动或误设单个环境变量不会启用自检或修改外观配置。

### 更新依赖锁

锁文件面向 Windows Python 3.13，包含运行、pytest、PyInstaller 6.21.0 及传递依赖的精确版本和哈希。修改 `requirements*.txt` 或打包器版本后，在 Python 3.13 环境执行：

```powershell
.\.venv-build\Scripts\python.exe -m pip install pip-tools==7.5.2
.\.venv-build\Scripts\python.exe -m piptools compile --generate-hashes --allow-unsafe --resolver=backtracking --output-file=requirements-lock.txt requirements-lock.in
```

提交 `requirements-lock.in` 与重新生成的 `requirements-lock.txt`，再完整构建和冒烟。

## 来源、致谢与许可证

PalDeck 感谢 Okaetsu 维护 Palworld 专用 [RE-UE4SS](https://github.com/Okaetsu/RE-UE4SS) 构建，也感谢 UE4SS-RE、Flask、pywebview、Pillow、PyInstaller 及 Palworld Modding Community。完整来源、版本和许可证可在应用“致谢”页查看；随包 UE4SS 的 `LICENSE` 与 `NOTICE.md` 位于 `third_party/ue4ss-palworld/`。

## License

MIT
