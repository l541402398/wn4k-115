@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo   wn4k-115  转存工作台
echo ============================================
echo.

python -m app.server

if errorlevel 1 (
  echo.
  echo [启动失败] 请先安装依赖:
  echo   python -m pip install -r requirements.txt
  echo.
  pause
)

endlocal