@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 重新打包 幻兽帕鲁Mod管理面板

echo 生成图标...
py -3 build_icon.py

echo 打包单文件 EXE（桌面窗口）...
py -3 -m PyInstaller --noconfirm --clean --onefile ^
  --name "幻兽帕鲁Mod管理面板" ^
  --icon "assets\app.ico" ^
  --add-data "frontend;frontend" ^
  --add-data "assets;assets" ^
  --hidden-import flask --hidden-import webview ^
  --hidden-import backend --hidden-import backend.app ^
  --hidden-import backend.game_detector --hidden-import backend.mod_manager --hidden-import backend.nexus_api ^
  --collect-all flask --collect-all webview ^
  --noconsole --windowed ^
  launcher.py

if errorlevel 1 (
  echo 打包失败
  pause
  exit /b 1
)

echo 复制到桌面...
copy /Y "dist\幻兽帕鲁Mod管理面板.exe" "%USERPROFILE%\Desktop\幻兽帕鲁Mod管理面板.exe" >nul
if errorlevel 1 copy /Y "dist\幻兽帕鲁Mod管理面板.exe" "%USERPROFILE%\OneDrive\Desktop\幻兽帕鲁Mod管理面板.exe" >nul

echo.
echo 完成：桌面 \ 幻兽帕鲁Mod管理面板.exe
pause
