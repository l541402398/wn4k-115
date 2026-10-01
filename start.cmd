@echo off
REM 启动转存工作台（Windows）
setlocal
cd /d "%~dp0"
echo ============================================
echo   蜗牛4K 精选 -^> 115 转存工作台
echo ============================================
python -m app.server
if errorlevel 1 (
  echo.
  echo 启动失败。请确认已安装依赖：
  echo   python -m pip install -r requirements.txt
  pause
)
endlocal