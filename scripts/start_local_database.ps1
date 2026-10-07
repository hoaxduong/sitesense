# Start an isolated development PostgreSQL using already installed binaries.
param(
    [string]$PostgresBin = 'D:\PostgreSQL\bin',
    [int]$Port = 55432
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$clusterPath = Join-Path $projectRoot 'data\interim\postgres'
$envPath = Join-Path $projectRoot '.env'
$pgCtl = Join-Path $PostgresBin 'pg_ctl.exe'
$initDb = Join-Path $PostgresBin 'initdb.exe'
if (!(Test-Path -LiteralPath $pgCtl) -or !(Test-Path -LiteralPath $initDb)) {
    throw 'PostgreSQL binaries not found. Pass -PostgresBin with the installed bin directory.'
}
if (Test-Path -LiteralPath (Join-Path $clusterPath 'PG_VERSION')) {
    & $pgCtl status -D $clusterPath
    $alreadyRunning = ($LASTEXITCODE -eq 0)
} else {
    if (Test-Path -LiteralPath $envPath) {
        throw 'An .env already exists. Use its configured database; this helper never replaces it.'
    }
    $probe = [System.Net.Sockets.TcpClient]::new()
    try {
        $probe.Connect('127.0.0.1', $Port)
        throw "Port $Port is already occupied. Choose a different -Port."
    } catch [System.Net.Sockets.SocketException] {
        # Connection refused: available for this development cluster.
    } finally {
        $probe.Dispose()
    }
    New-Item -ItemType Directory -Force -Path $clusterPath | Out-Null
    $passwordFile = Join-Path $projectRoot 'data\interim\postgres-password.tmp'
    $localPassword = [Guid]::NewGuid().ToString('N') + [Guid]::NewGuid().ToString('N')
    try {
        [System.IO.File]::WriteAllText($passwordFile, $localPassword)
        & $initDb -D $clusterPath -U sitesense --auth-host=scram-sha-256 --auth-local=scram-sha-256 --encoding=UTF8 --locale=C --pwfile=$passwordFile
        if ($LASTEXITCODE -ne 0) { throw 'Database initialization failed.' }
        $configuration = @"
POSTGRES_DB=sitesense
POSTGRES_USER=sitesense
POSTGRES_PASSWORD=$localPassword
POSTGRES_PORT=$Port
DATABASE_URL=postgresql://sitesense:$localPassword@127.0.0.1:$Port/sitesense
"@
        [System.IO.File]::WriteAllText($envPath, $configuration + [Environment]::NewLine)
    } finally {
        Remove-Item -LiteralPath $passwordFile -ErrorAction SilentlyContinue
        $localPassword = $null
    }
}
$serverLog = Join-Path $projectRoot 'data\interim\postgres.log'
$arguments = @('start', '-D', ('"' + $clusterPath + '"'), '-l', ('"' + $serverLog + '"'),
               '-o', ('"-h 127.0.0.1 -p ' + $Port + '"'), '-w')
if (!$alreadyRunning) {
    $process = Start-Process -FilePath $pgCtl -ArgumentList $arguments -WindowStyle Hidden -PassThru
    if (!$process.WaitForExit(30000)) { throw 'Database startup timed out. Inspect the server log.' }
    if ($process.ExitCode -ne 0) { throw 'Database startup failed. Inspect data/interim/postgres.log.' }
}
$previousPgPassword = $env:PGPASSWORD
try {
    $passwordLine = Get-Content -LiteralPath $envPath | Where-Object { $_.StartsWith('POSTGRES_PASSWORD=') }
    if (!$passwordLine) { throw 'Missing POSTGRES_PASSWORD in .env.' }
    $env:PGPASSWORD = $passwordLine.Substring('POSTGRES_PASSWORD='.Length)
    $exists = & (Join-Path $PostgresBin 'psql.exe') -w -h 127.0.0.1 -p $Port -U sitesense -d postgres -Atc "SELECT 1 FROM pg_database WHERE datname = 'sitesense'"
    if ($LASTEXITCODE -ne 0) { throw 'Could not connect to the development cluster.' }
    if ($exists -ne '1') {
        & (Join-Path $PostgresBin 'createdb.exe') -w -h 127.0.0.1 -p $Port -U sitesense sitesense
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the sitesense database.' }
    }
} finally {
    $env:PGPASSWORD = $previousPgPassword
}
Write-Output "Local PostgreSQL started on 127.0.0.1:$Port. Configuration is in ignored .env."
