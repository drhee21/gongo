$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Set-Location -Path $PSScriptRoot

if (!(Test-Path ".\.venv")) {
  python -m venv .venv
}
. .\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python .\collector.py
