@echo off
rem HFK 启动器：优先使用 HFK 自己的虚拟环境，其次使用 Hermes 自带的虚拟环境。
setlocal
set "HFK_ROOT=%~dp0"
set "HFK_PY=%HFK_ROOT%.venv\Scripts\python.exe"

if not exist "%HFK_PY%" (
  set "HFK_PY=%LOCALAPPDATA%\hermes\hermes-agent\venv\Scripts\python.exe"
)

if not exist "%HFK_PY%" (
  echo [HFK] 找不到可用的 Python 解释器。
  echo       请先运行 install.ps1，或手动创建虚拟环境：
  echo       python -m venv .venv ^&^& .venv\Scripts\pip install ruamel.yaml==0.18.17
  exit /b 2
)

set "PYTHONPATH=%HFK_ROOT%"
"%HFK_PY%" -m hfk.cli %*
exit /b %ERRORLEVEL%
