@echo off
rem Game Journal launcher. Creates the virtual environment on first run.
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" goto run
echo First run: creating virtual environment...
py -m venv .venv
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
if errorlevel 1 goto fail
:run
echo.
echo Game Journal: http://localhost:5000   (Ctrl+C to stop)
echo.
".venv\Scripts\python.exe" app.py
pause
goto :eof
:fail
echo.
echo Setup failed. Make sure Python 3.11+ is installed and "py" works in cmd.
if exist ".venv" rmdir /s /q ".venv"
pause