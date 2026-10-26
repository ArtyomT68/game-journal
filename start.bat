@echo off
rem Game Journal launcher.
rem  - Portable build (GameJournal-...-windows.zip from GitHub Releases): runs the bundled
rem    python\python.exe, nothing to install.
rem  - Source code: creates .venv on first run, reinstalls packages when requirements.txt changes.
cd /d "%~dp0"
if exist "python\python.exe" goto portable

if exist ".venv\Scripts\python.exe" goto deps
where py >nul 2>&1
if errorlevel 1 goto nopython
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

:nopython
echo.
echo  This folder contains only the SOURCE CODE, and Python is not installed.
echo.
echo  To run Game Journal without installing anything:
echo    1. Open the Releases page of the project on GitHub.
echo    2. In "Assets" download GameJournal-...-windows.zip
echo       (NOT "Source code (zip)").
echo    3. Unpack it and run start.bat from there.
echo.
echo  Or install Python 3.11+ from python.org and run this start.bat again.
echo.
pause
goto :eof

:fail
echo.
echo Setup failed. Make sure Python 3.11+ is installed and "py" works in cmd,
echo or download GameJournal-...-windows.zip from the Releases page on GitHub.
if exist ".venv" rmdir /s /q ".venv"
pause