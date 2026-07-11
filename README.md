# PalDeck — 幻兽帕鲁 Mod 管理器

PalDeck 是面向 **Windows 10/11、Steam 客户端版 Palworld** 的便携桌面 Mod 管理器。程序使用本机 Flask 服务与 pywebview 窗口，配置、清单、背景和缓存保存在 `PalDeck.exe` 同目录的 `data` 文件夹。

## 下载与运行

从 [Releases](https://github.com/ylty1516/palworld-mod-manager/releases) 下载 `PalDeck-v2.0.0-windows-portable.zip`，完整解压后双击 `PalDeck-portable/PalDeck.exe`。建议安装 [Microsoft Edge WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/)；Windows 11 通常已自带。

应用可检查 GitHub Release、下载更新并在退出后替换当前程序；可识别新的 `PalDeck.exe` / PalDeck 便携 ZIP，旧版 `PalMod.exe` 资源仍保持兼容。

## 支持范围与真实功能

- 自动检测 Steam 库中的 Palworld，也可手动选择经过验证的游戏目录。
- 导入 `.zip` 或 `.pak`，识别并安装普通 PAK、LogicMods 和 UE4SS Lua/脚本 Mod。
- 直接导入 PAK 时处理同名 `.pak` / `.utoc` / `.ucas` 文件组。
- 通过受管理清单启用、禁用和删除 Mod，并检查磁盘文件状态。
- 匿名、只读浏览和搜索 Nexus Mods，显示图片、作者、版本、统计与 Nexus ID。
- 三套主题：极光玻璃、象牙樱花、星夜；支持自定义背景、遮罩、模糊、位置和动态樱花密度。
- 绿色便携数据目录，不需要安装到系统目录。

## 明确不支持

- Xbox / Microsoft Store 版 Palworld。
- Palworld 专用服务器 Mod 管理。
- Steam Workshop 的订阅、取消订阅或缓存修改。
- Nexus 登录、下载或自动安装；Nexus 功能仅为匿名只读目录。

## 风险与备份

Mod 会修改游戏目录，第三方 Mod 也可能不兼容当前游戏版本。**安装、启停或删除 Mod 前，请备份 Palworld 存档和相关游戏文件。** 不要在 Palworld 运行时操作 Mod；遇到异常时先禁用 Mod，并使用 Steam 校验游戏文件。PalDeck 不能保证第三方 Mod 的安全性或兼容性。

## 从源码运行

需要 Python 3.10+、Node.js（用于前端语法检查）和 Microsoft Edge WebView2 Runtime：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt
.\.venv\Scripts\python.exe launcher.py
```

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
Get-ChildItem frontend -Filter *.js | ForEach-Object { node --check $_.FullName }
.\.venv\Scripts\python.exe -m compileall -q backend launcher.py scripts
```

## 构建 Windows 便携版

构建必须从 F 盘的项目工作区执行。脚本固定仓库根目录，将所有临时目录与缓存放在项目内，创建或复用 `.venv-build`，先运行完整验证，再生成 one-file/windowed 产物：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build_portable.ps1
# 或
.\build_exe.bat
```

输出：

- `dist/PalDeck-portable/PalDeck.exe`
- `dist/PalDeck-portable/README.txt`
- `dist/PalDeck-v2.0.0-windows-portable.zip`
- `dist/PalDeck-v2.0.0-windows-portable.zip.sha256`

构建脚本不会复制文件到桌面。

## License

MIT
