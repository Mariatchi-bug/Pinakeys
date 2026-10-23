@echo off
setlocal
set "ROOT=%~dp0"
set "VENV_PYTHON=%LOCALAPPDATA%\USBBookIndex\venv\Scripts\python.exe"
if exist "%VENV_PYTHON%" (
    "%VENV_PYTHON%" "%ROOT%audit_materials.py" %*
) else (
    where py >nul 2>nul
    if not errorlevel 1 (
        py -3 "%ROOT%audit_materials.py" %*
    ) else (
        python "%ROOT%audit_materials.py" %*
    )
)
if errorlevel 1 (
    echo.
    echo Audit failed. Check README.md.
    pause
    exit /b 1
)
echo.
echo Audit complete. See the reports location printed above.
pause
