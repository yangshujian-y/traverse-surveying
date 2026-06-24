@echo off
setlocal
cd /d "%~dp0"

echo Installing dependencies...
py -3 -m pip install --upgrade pip
py -3 -m pip install -r requirements.txt

echo Building Windows executable...
py -3 -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --windowed ^
  --onefile ^
  --name "归档图片批量复制与页码工具" ^
  archive_image_tool.py

echo.
echo Build finished.
echo EXE path: %cd%\dist\归档图片批量复制与页码工具.exe
pause
