$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

if (-not $env:POS_APP_PASSWORD) {
    $env:POS_APP_PASSWORD = "1234"
}

if (-not $env:POS_SECRET_KEY) {
    $env:POS_SECRET_KEY = "pos-system-local-secret"
}

python serve.py
