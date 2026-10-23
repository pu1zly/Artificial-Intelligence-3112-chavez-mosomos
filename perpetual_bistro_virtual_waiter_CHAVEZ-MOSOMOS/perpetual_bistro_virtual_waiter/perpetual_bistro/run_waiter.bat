@echo off
setlocal
cd /d "%~dp0"
title Perpetual Bistro Virtual Waiter

rem Usage: run_waiter.bat          start (resumes the saved order)
rem        run_waiter.bat reset    confirm, wipe saved order and restore stock, then start
set FLAGS=
if /i "%~1"=="reset" goto CONFIRM_RESET
goto CHECK_PYTHON

:CONFIRM_RESET
echo This erases the saved order and restores menu stock.
choice /m "Continue"
if errorlevel 2 exit /b
set FLAGS=--reset-db

:CHECK_PYTHON

where python >nul 2>nul
if errorlevel 1 (
  echo [X] Python was not found. Install it from https://python.org and tick "Add to PATH".
  pause & exit /b 1
)

if not exist ".venv\Scripts\activate.bat" (
  echo Creating virtual environment...
  python -m venv .venv
)
call ".venv\Scripts\activate.bat"
echo Checking Python packages...
python -m pip install -q -r requirements.txt
if errorlevel 1 ( echo [X] Package install failed. & pause & exit /b 1 )

where ollama >nul 2>nul
if errorlevel 1 (
  echo [X] Ollama was not found. Install it from https://ollama.com then run this again.
  pause & exit /b 1
)

ollama list >nul 2>nul
if errorlevel 1 (
  echo Starting Ollama...
  start "Ollama" /min ollama serve
)

set /a WAIT=0
:WAIT_FOR_OLLAMA
ollama list >nul 2>nul
if not errorlevel 1 goto OLLAMA_READY
set /a WAIT+=1
if %WAIT% geq 15 (
  echo [X] Ollama did not become ready. Start Ollama and try again.
  pause & exit /b 1
)
timeout /t 2 /nobreak >nul
goto WAIT_FOR_OLLAMA

:OLLAMA_READY
python -c "import ollama,sys; sys.exit(0 if ollama.list().models else 1)" >nul 2>nul
if errorlevel 1 (
  echo No installed Ollama models found. Downloading Qwen 3 as a tool-capable default...
  ollama pull qwen3:8B
  if errorlevel 1 (
    echo [X] Could not download a model. Check your Ollama connection and try again.
    pause & exit /b 1
  )
)

echo.
echo Starting the Perpetual Bistro Tkinter kiosk...
python virtual_waiter_prototype.py %FLAGS%
