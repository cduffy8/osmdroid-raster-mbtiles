@echo off
setlocal
set "PYTHON_MANAGER_AUTOMATIC_INSTALL=false"
where py >nul 2>&1
if not errorlevel 1 goto with_py
where python >nul 2>&1
if not errorlevel 1 goto with_python
echo Python 3 is required. See docs/windows.md for offline installation.
set "converter_exit=1"
goto finished
:with_py
py -3 "%~dp0osmdroid_to_mbtiles.py" folder-wizard
set "converter_exit=%errorlevel%"
goto finished
:with_python
python "%~dp0osmdroid_to_mbtiles.py" folder-wizard
set "converter_exit=%errorlevel%"
:finished
echo.
pause
exit /b %converter_exit%
