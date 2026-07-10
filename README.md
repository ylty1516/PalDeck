# 幻兽帕鲁 Mod 管理面板

独立桌面窗口版（蓝白主题），用于管理 [幻兽帕鲁 / Palworld](https://store.steampowered.com/app/1623730) 模组。

## 下载

请到 [Releases](https://github.com/ylty1516/palworld-mod-manager/releases) 下载最新 zip，解压后双击 `帕鲁Mod.exe` 即可。

## 功能

- 自动检测 Steam 中的幻兽帕鲁安装目录
- 创建/定位 `~mods`、LogicMods、UE4SS、Workshop 目录
- 导入 `.zip` / `.pak` 并自动安装
- 每个模组启用 / 禁用开关
- 实时连接 Nexus Mods（N 网）热门与搜索，显示模组尾号
- 独立桌面窗口（WebView2），无需系统浏览器

## 使用说明

1. 从 Release 下载并解压
2. 双击 **`帕鲁Mod.exe`**
3. 首次启动会自动检测游戏路径；失败可在「游戏路径」中手动设置
4. 关闭窗口即退出

配置保存在 exe 同目录下的 `data` 文件夹。

## 开发 / 重新打包

需要 Python 3.10+：

```bat
py -3 -m pip install -r requirements.txt pyinstaller pillow
build_exe.bat
```

## 系统要求

- Windows 10 / 11
- 建议已安装 [Microsoft Edge WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/)（Win11 通常自带）

## License

MIT
