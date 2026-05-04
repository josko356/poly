@echo off
setlocal enabledelayedexpansion

echo.
echo ============================================================
echo    Polymarket Latency Arbitrage Bot -- Windows Setup
echo ============================================================
echo.

:: ── Check Python ──────────────────────────────────────────────
echo [1/5] Checking Python version...
where python >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found in PATH.
    echo Download Python 3.11+ from https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during installation.
    pause
    exit /b 1
)

for /f "tokens=2" %%i in ('python --version 2^>^&1') do set PYVER=%%i
echo OK: Python %PYVER% found.

:: Check version is 3.11+
python -c "import sys; exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>&1
if errorlevel 1 (
    echo WARNING: Python 3.11+ recommended. Current: %PYVER%
    echo The bot may still work but is not tested on older versions.
)

:: ── Virtual environment ────────────────────────────────────────
echo.
echo [2/5] Setting up virtual environment...
if exist venv\ (
    echo venv\ already exists -- skipping creation.
) else (
    python -m venv venv
    if errorlevel 1 (
        echo ERROR: Failed to create virtual environment.
        pause
        exit /b 1
    )
    echo OK: venv\ created.
)

call venv\Scripts\activate.bat
if errorlevel 1 (
    echo ERROR: Could not activate venv.
    pause
    exit /b 1
)
echo OK: Virtual environment activated.

:: ── Install packages ──────────────────────────────────────────
echo.
echo [3/5] Installing Python packages...
python -m pip install --upgrade pip --quiet
python -m pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo ERROR: Package installation failed.
    echo Try running: pip install -r requirements.txt
    pause
    exit /b 1
)
echo OK: All packages installed.

:: ── .env setup ────────────────────────────────────────────────
echo.
echo [4/5] Setting up .env file...
if exist .env (
    echo .env already exists -- skipping. Edit it manually if needed.
) else (
    copy .env.example .env >nul
    echo OK: .env created from template.
    echo.
    echo ===========================================================
    echo  IMPORTANT: Edit .env with your credentials:
    echo.
    echo  COINBASE_API_KEY_NAME   -- from coinbase.com/settings/api
    echo  COINBASE_API_PRIVATE_KEY -- EC private key from Coinbase
    echo  TELEGRAM_BOT_TOKEN      -- from @BotFather on Telegram
    echo  TELEGRAM_CHAT_ID        -- from @userinfobot on Telegram
    echo.
    echo  Wallet keys only needed when enabling live trading.
    echo ===========================================================
    echo.
    set /p OPEN="Open .env in Notepad now? [Y/n]: "
    if /i "!OPEN!" neq "n" (
        notepad .env
    )
)

:: ── Contract check ────────────────────────────────────────────
echo.
echo [5/5] Checking Polymarket for active contracts...
python scripts\check_contracts.py
if errorlevel 1 (
    echo NOTE: Contract check failed -- check your internet connection.
    echo       You can run it manually: python scripts\check_contracts.py
)

:: ── Done ──────────────────────────────────────────────────────
echo.
echo ============================================================
echo    Setup complete!
echo ============================================================
echo.
echo  Start paper trading:
echo    venv\Scripts\activate
echo    python main.py
echo.
echo  Check active contracts:
echo    python scripts\check_contracts.py
echo.
echo  Run tests:
echo    python scripts\run_tests.py
echo.
echo  View trade stats:
echo    python main.py --stats
echo.
pause
