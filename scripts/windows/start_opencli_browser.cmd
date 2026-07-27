@echo off
setlocal
for %%I in ("%~dp0..\..") do set "PROJECT_ROOT=%%~fI"
cd /d "%PROJECT_ROOT%"

set "BROWSER=C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
set "PROFILE=%PROJECT_ROOT%\tools\opencli\edge-headless-profile"
set "EXTENSION=%PROJECT_ROOT%\tools\opencli\extension"

if not exist "%BROWSER%" goto edge_missing

if not exist "%EXTENSION%\manifest.json" goto extension_missing

if not exist "%PROFILE%" mkdir "%PROFILE%"

start "OpenCLI Browser Bridge" /min "%BROWSER%" --start-minimized --user-data-dir="%PROFILE%" --disable-extensions-except="%EXTENSION%" --load-extension="%EXTENSION%" --no-first-run --disable-default-apps about:blank

echo.
echo OpenCLI minimized Edge bridge started.
echo Run scripts\windows\check_opencli.cmd to verify the Browser Bridge connection.
endlocal
exit /b 0

:edge_missing
echo Microsoft Edge was not found at:
echo "%BROWSER%"
endlocal
exit /b 1

:extension_missing
echo OpenCLI Browser Bridge extension was not found at:
echo "%EXTENSION%"
endlocal
exit /b 1
