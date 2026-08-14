@echo off
cd /d "%~dp0"

pip install -r requirements.txt -q

:loop
python app.py
timeout /t 2 >nul
goto loop
