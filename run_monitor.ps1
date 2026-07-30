# Wrapper for Windows Task Scheduler. Runs the monitor with the folder's venv
# if present, else system python. Logs go into data\monitor.log via the script.
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Definition
$venvPy = Join-Path $here ".venv\Scripts\python.exe"
$py = if (Test-Path $venvPy) { $venvPy } else { "python" }
& $py (Join-Path $here "flight_monitor.py")
