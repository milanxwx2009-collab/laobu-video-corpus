@echo off
chcp 65001 >nul
setlocal
set "PYTHONUTF8=1"
set "LAOBU_PYTHON=%CODEX_PYTHON%"
if not defined LAOBU_PYTHON set "LAOBU_PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if not defined CODEX_PYTHON if exist "%~dp0python_path.txt" set /p "LAOBU_PYTHON="<"%~dp0python_path.txt"
if not exist "%LAOBU_PYTHON%" set "LAOBU_PYTHON=python"
"%LAOBU_PYTHON%" -X utf8 "%~dp0run.py" scan
if errorlevel 1 goto end
"%LAOBU_PYTHON%" -X utf8 "%~dp0run.py" process --limit 1
:end
pause
