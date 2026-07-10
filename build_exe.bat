@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 重新打包 幻兽帕鲁Mod管理面板

echo 生成图标...
py -3 build_icon.py

echo 打包单文件 EXE（桌面窗口）...
py -3 -m PyInstaller --noconfirm --clean --onefile ^
  --name "PalMod" ^
  --icon "assets\app.ico" ^
  --add-data "frontend;frontend" ^
  --add-data "assets;assets" ^
  --add-data "bundled_mods;bundled_mods" ^
  --hidden-import flask --hidden-import webview ^
  --hidden-import backend --hidden-import backend.app ^
  --hidden-import backend.game_detector --hidden-import backend.mod_manager ^
  --hidden-import backend.nexus_api --hidden-import backend.ue4ss_installer ^
  --hidden-import backend.mod_config ^
  --collect-all flask --collect-all webview ^
  --noconsole --windowed ^
  launcher.py

if errorlevel 1 (
  echo 打包失败
  pause
  exit /b 1
)

echo 复制到桌面...
copy /Y "dist\PalMod.exe" "%USERPROFILE%\Desktop\帕鲁Mod.exe" >nul
if errorlevel 1 copy /Y "dist\PalMod.exe" "%USERPROFILE%\OneDrive\Desktop\帕鲁Mod.exe" >nul

echo.
echo 完成：桌面 \ 帕鲁Mod.exe
pause
