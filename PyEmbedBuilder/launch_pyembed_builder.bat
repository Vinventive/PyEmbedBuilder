@echo off
setlocal EnableExtensions
set "ROOT=%~dp0"
pushd "%ROOT%"
start "" "%ROOT%python-embed\pythonw.exe" "%ROOT%python-embed\run_pyembed_builder.py"
popd
endlocal
exit /b
