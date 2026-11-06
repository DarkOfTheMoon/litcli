@echo off
rem In-repo launcher for Windows (the counterpart of bin/litcli): puts the project root on
rem PYTHONPATH and forwards to the module. Production installs use `pip install .` instead.
set "PYTHONPATH=%~dp0..;%PYTHONPATH%"
python -m litcli.cli %*
