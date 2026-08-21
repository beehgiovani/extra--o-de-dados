@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Ambiente Python nao encontrado em .venv\Scripts\python.exe
  pause
  exit /b 1
)
".venv\Scripts\python.exe" "servidor_conferencia_mogi.py"
if errorlevel 1 pause
