@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
)

call .venv\Scripts\activate.bat
python -m pip install -q --upgrade pip
pip install -q -r requirements.txt
pip install -q pyinstaller pillow certifi

if not exist "assets" mkdir assets
python make_icon.py

pyinstaller --noconfirm --clean build.spec
if errorlevel 1 exit /b 1

set "RELEASE=release\NaverOwnership"
if exist "release" rmdir /s /q "release"
mkdir "%RELEASE%"
xcopy /E /I /Y "dist\NaverOwnership\*" "%RELEASE%\" >nul

if not exist "%RELEASE%\data" mkdir "%RELEASE%\data"

powershell -NoProfile -Command ^
  "$p=Join-Path '%CD%' '%RELEASE%\Start.bat';" ^
  "@echo off`r`ncd /d `"%%~dp0`"`r`nstart `"`" `"NaverOwnership.exe`"`r`n" | Set-Content -Path $p -Encoding ASCII

endlocal
