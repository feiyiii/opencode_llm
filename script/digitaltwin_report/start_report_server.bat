@echo off
setlocal
cd /d %~dp0

if exist local_env.bat (
  call local_env.bat
)

if "%REPORT_LLM_ENABLED%"=="" set REPORT_LLM_ENABLED=1
if "%REPORT_LLM_BASE_URL%"=="" set REPORT_LLM_BASE_URL=https://api.deepseek.com/v1
if "%REPORT_LLM_MODEL%"=="" set REPORT_LLM_MODEL=deepseek-chat
if "%REPORT_PORT%"=="" set REPORT_PORT=8003

echo Starting report server on port %REPORT_PORT% ...
python report_server.py

endlocal
