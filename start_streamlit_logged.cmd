@echo off
setlocal
cd /d "%~dp0"

netstat -ano | findstr /R /C:":8501 .*LISTENING" >nul
if not errorlevel 1 (
    echo ExpertSearch Streamlit is already running at http://localhost:8501/
    echo Stop the existing terminal with Ctrl+C before starting a new instance.
    endlocal
    exit /b 0
)

echo [1/3] Starting OpenCLI browser bridge...
call start_opencli_browser.cmd
if errorlevel 1 (
    echo [WARN] OpenCLI browser bridge failed to start. Streamlit will continue without browser fallback.
    goto start_streamlit
)

echo [2/3] Waiting for OpenCLI Browser Bridge...
set /a OPENCLI_WAIT_COUNT=0

:wait_opencli
set /a OPENCLI_WAIT_COUNT+=1
call check_opencli.cmd 2>nul | findstr /L /C:"[OK] Connectivity" >nul
if not errorlevel 1 goto opencli_ready
if %OPENCLI_WAIT_COUNT% GEQ 5 goto opencli_failed
timeout /t 2 /nobreak >nul
goto wait_opencli

:opencli_failed
echo [WARN] OpenCLI Browser Bridge did not become healthy within 10 seconds.
echo [WARN] Homepage access will use HTTP first and record a structured failure if OpenCLI is unavailable.
goto start_streamlit

:opencli_ready
echo OpenCLI Browser Bridge is healthy.

:start_streamlit
set "PYTHON=%~dp0.venv\Scripts\python.exe"

if not exist "%PYTHON%" (
    echo Project virtual environment Python was not found:
    echo "%PYTHON%"
    exit /b 1
)

echo [3/3] Starting Streamlit...
echo URL: http://localhost:8501
echo Log: %~dp0streamlit.log
echo Keep this terminal open. Press Ctrl+C to stop the system.

if not exist "%~dp0logs" mkdir "%~dp0logs"
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "LOGSTAMP=%%i"
if exist "%~dp0streamlit.log" move /y "%~dp0streamlit.log" "%~dp0logs\streamlit_%LOGSTAMP%.log" >nul
if exist "%~dp0streamlit.error.log" move /y "%~dp0streamlit.error.log" "%~dp0logs\streamlit_error_%LOGSTAMP%.log" >nul

"%PYTHON%" -m streamlit run app.py > streamlit.log 2>&1

echo.
echo Streamlit stopped. Check streamlit.log for details.
endlocal
