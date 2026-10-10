@echo off
cd /d "%~dp0"
python -m venv .venv
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
echo Setup complete. Open Start Luma.cmd.
pause
exit /b 0
:failed
echo Setup failed. Python 3.11 or later and internet access are required.
pause
exit /b 1
