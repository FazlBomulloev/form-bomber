@echo off
setlocal
chcp 65001 > nul

echo === Form Bomber - Install ===
echo.

where py >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python launcher "py" not found.
    echo Install Python 3.11+ from python.org and retry.
    pause
    exit /b 1
)

echo [1/4] Creating virtual environment...
if not exist .venv (
    py -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create .venv
        pause
        exit /b 1
    )
) else (
    echo .venv already exists, skipping creation.
)

echo [2/4] Installing Python dependencies...
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip --quiet
if errorlevel 1 (
    echo [ERROR] pip upgrade failed
    pause
    exit /b 1
)

python -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] pip install -r requirements.txt failed
    pause
    exit /b 1
)

echo [3/4] Installing Playwright browser (Chromium)...
python -m playwright install chromium
if errorlevel 1 (
    echo [ERROR] playwright install chromium failed
    pause
    exit /b 1
)

echo [4/4] Ensuring data folder exists...
if not exist data mkdir data

echo.
echo === Install completed ===
echo You can now run: start_checker_ai.bat
echo.
pause
