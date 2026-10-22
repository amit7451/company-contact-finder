@echo off
setlocal
cd /d "%~dp0"

echo Company Contact Finder
where py >nul 2>nul
if errorlevel 1 (
  echo Python launcher ^(py^) was not found. Install Python 3.11+ and enable Add Python to PATH.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  py -3 -m venv .venv
  if errorlevel 1 goto :failed
)

echo Installing or checking dependencies...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :failed
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :failed

echo Starting local server at http://127.0.0.1:8000
start "" /B cmd /c "timeout /t 3 /nobreak >nul & start http://127.0.0.1:8000"
echo Leave this window open while using the app. Press Ctrl+C to stop the server.
".venv\Scripts\python.exe" -m uvicorn app:app --host 127.0.0.1 --port 8000
exit /b 0

:failed
echo.
echo Setup failed. Check the error above and try again.
pause
exit /b 1
