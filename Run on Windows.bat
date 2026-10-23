@echo off
setlocal
set "ROOT=%~dp0"
set "VENV_PYTHON=%LOCALAPPDATA%\USBBookIndex\venv\Scripts\python.exe"
if exist "%VENV_PYTHON%" (
    "%VENV_PYTHON%" "%ROOT%scan_library.py" %*
) else (
    where py >nul 2>nul
    if not errorlevel 1 (
        py -3 "%ROOT%scan_library.py" %*
    ) else (
        python "%ROOT%scan_library.py" %*
    )
)
if errorlevel 1 (
    echo.
    echo The scan did not complete. Check the message above and README.md.
    pause
    exit /b 1
)
echo.
echo Scan complete. Open the catalogue named above.
pause
