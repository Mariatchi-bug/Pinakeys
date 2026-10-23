@echo off
setlocal
set "VENV=%LOCALAPPDATA%\USBBookIndex\venv"
where py >nul 2>nul
if not errorlevel 1 (
    py -3 -m venv "%VENV%"
) else (
    python -m venv "%VENV%"
)
if errorlevel 1 goto fail
"%VENV%\Scripts\python.exe" -m pip install pymupdf==1.28.2
if errorlevel 1 goto fail
echo.
echo Ready. Use Run on Windows.bat. Scanning works offline from now on.
pause
exit /b 0
:fail
echo Setup failed. Install Python 3 first and make sure you have internet access.
pause
exit /b 1
