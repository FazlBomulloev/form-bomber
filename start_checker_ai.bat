@echo off
setlocal
chcp 65001 > nul

echo === Form Bomber - Start ===
echo.

if not exist .venv\Scripts\activate.bat (
    echo [ERROR] Virtual environment not found.
    echo Run install.bat first.
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat

echo Starting server...
echo Press Ctrl+C to stop.
echo.

python src/app.py

pause
