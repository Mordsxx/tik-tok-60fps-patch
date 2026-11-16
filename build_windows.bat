@echo off
REM Build a standalone 64-bit EXE.  Requires 64-bit Python 3.13 from python.org.
cd /d "%~dp0"
py -3.13 -m venv .venv || goto :err
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt || goto :err
REM Optional: copy ffprobe.exe next to main.py first; it will be bundled for validation.
set EXTRA=
if exist ffprobe.exe set EXTRA=--add-binary "ffprobe.exe;."
pyinstaller --noconfirm --onefile --noconsole --name "TikTok 60FPS Patch" ^
  --collect-all tkinterdnd2 %EXTRA% main.py || goto :err
echo.
echo Done: dist\TikTok 60FPS Patch.exe
exit /b 0
:err
echo BUILD FAILED
exit /b 1
