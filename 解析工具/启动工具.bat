@echo off
chcp 65001 >nul
setlocal
set "PYTHONUTF8=1"
set "LAOBU_PYTHON=%CODEX_PYTHON%"
if not defined LAOBU_PYTHON set "LAOBU_PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if not defined CODEX_PYTHON if exist "%~dp0python_path.txt" set /p "LAOBU_PYTHON="<"%~dp0python_path.txt"
if not exist "%LAOBU_PYTHON%" set "LAOBU_PYTHON=python"
pushd "%~dp0"
if "%~1"=="" goto help
"%LAOBU_PYTHON%" -X utf8 "%~dp0run.py" %*
set "LAOBU_EXIT=%ERRORLEVEL%"
popd
exit /b %LAOBU_EXIT%
:help
"%LAOBU_PYTHON%" -X utf8 "%~dp0run.py" --help
echo Example: process --limit 1
pause
popd
