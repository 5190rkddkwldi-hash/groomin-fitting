@echo off
cd /d "%~dp0"

echo Installing dependencies (first run may take a minute)...
pip install -r requirements.txt -q

start "" cmd /c "timeout /t 2 >nul && start http://127.0.0.1:5000"

echo.
echo Starting server. Your browser will open automatically.
echo Close this window to stop the server.
echo.

:loop
python app.py
echo.
echo Server stopped unexpectedly - restarting in 2 seconds...
echo (close this window to quit for real)
timeout /t 2 >nul
goto loop
