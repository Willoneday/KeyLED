@echo off
setlocal enableextensions
title KeyLED - rebuild exe

rem Rebuilds KeyLED.exe from app\keyled.py + app\ui.html.
rem Only needed if you want to change the code. The shipped KeyLED.exe
rem already works: just double-click it.

cd /d "%~dp0.."

where python >nul 2>nul
if errorlevel 1 goto nopython

echo.
echo   Installing build dependencies (first run only)...
echo.
python -m pip install --quiet --disable-pip-version-check --index-url https://pypi.org/simple/ pyinstaller pywebview pystray pillow numpy pyaudiowpatch
if errorlevel 1 goto pipfail

echo   Building...
echo.
python -m PyInstaller --noconfirm --clean --onefile --noconsole ^
  --name KeyLED ^
  --icon "%~dp0keyled.ico" ^
  --add-data "%~dp0ui.html;." ^
  --add-data "%~dp0love.bin;." ^
  --add-data "%~dp0arrow.png;." ^
  --add-data "%~dp0hand.png;." ^
  --add-data "%~dp0text.png;." ^
  --hidden-import numpy ^
  --collect-all webview ^
  --collect-all pystray ^
  --collect-all pyaudiowpatch ^
  --distpath "%~dp0..\dist" ^
  --workpath "%~dp0..\build\pywork" ^
  --specpath "%~dp0..\build" ^
  "%~dp0keyled.py"

if errorlevel 1 goto buildfail

copy /y "%~dp0..\dist\KeyLED.exe" "%~dp0..\KeyLED.exe" >nul
echo.
echo   Done: KeyLED.exe next to this folder.
echo.
pause
exit /b 0

:nopython
echo.
echo   Python was not found on PATH.
echo   The shipped KeyLED.exe does not need Python - only rebuilding does.
echo   To rebuild: install Python 3.11+ from https://www.python.org/downloads/
echo   and tick "Add python.exe to PATH".
echo.
pause
exit /b 1

:pipfail
echo.
echo   Could not install build dependencies (no network?).
echo   The shipped KeyLED.exe still works as is.
echo.
pause
exit /b 1

:buildfail
echo.
echo   Build failed. The errors are above.
echo.
pause
exit /b 1
