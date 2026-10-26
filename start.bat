@echo off
rem Game Journal launcher.
rem  - Portable build (from GitHub Releases): runs the bundled python\python.exe, nothing to install.
rem  - Source code: creates .venv on first run, reinstalls packages when requirements.txt changes.
cd /d "%~dp0"
if exist "python\python.exe" goto portable

if exist ".venv\Scripts\python.exe" goto deps
echo First run: creating virtual environment...
py -m venv .venv
if errorlevel 1 goto fail

:deps
fc /b requirements.txt ".venv\requirements.installed" >nul 2>&1
if not errorlevel 1 goto run
echo Installing dependencies...
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
if errorlevel 1 goto fail
copy /y requirements.txt ".venv\requirements.installed" >nul

:run
".venv\Scripts\python.exe" app.py
pause
goto :eof

:portable
"python\python.exe" app.py
pause
goto :eof

:fail
echo.
echo Setup failed. Make sure Python 3.11+ is installed and "py" works in cmd,
echo or download the portable build from the Releases page on GitHub.
if exist ".venv" rmdir /s /q ".venv"
pause