# Run the app against the PostgreSQL connection configured in ignored .env.
param([int]$Port = 8501)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
if (!(Test-Path -LiteralPath '.env')) {
    throw 'Create .env from .env.example and configure DATABASE_URL before starting.'
}
$uvCommand = Get-Command uv -ErrorAction SilentlyContinue
if ($uvCommand) {
    $uvPath = $uvCommand.Source
} else {
    $uvPath = Join-Path $projectRoot 'data\interim\tools\uv.exe'
    if (!(Test-Path -LiteralPath $uvPath)) {
        throw 'Install uv and add it to PATH before starting.'
    }
}
# Honor the project-local connection even when a shell has an inherited URL.
$previousDatabaseUrl = $env:DATABASE_URL
try {
    Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue
    & $uvPath run --frozen --env-file .env python -m sitesense.database migrate
    if ($LASTEXITCODE -ne 0) { throw 'Database migrations failed; the app was not started.' }
    & $uvPath run --frozen --env-file .env streamlit run app.py --server.address=127.0.0.1 --server.port=$Port --server.headless=true
    if ($LASTEXITCODE -ne 0) { throw 'Streamlit exited with an error.' }
} finally {
    $env:DATABASE_URL = $previousDatabaseUrl
}
