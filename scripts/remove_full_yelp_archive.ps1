# Remove only the original source files after verifying the retained compact bundle.
# Close the archive JSON editor tabs first; Windows refuses files held by PyCharm.
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$archivePath = Join-Path $projectRoot 'archive'
$bundlePath = Join-Path $projectRoot 'data\local\yelp_subset'
if (!(Test-Path -LiteralPath $archivePath)) {
    Write-Output 'The original archive is already absent.'
    exit 0
}
$resolvedArchive = (Resolve-Path -LiteralPath $archivePath).ProviderPath
if ($resolvedArchive -ne $archivePath) { throw 'Unexpected archive path.' }
$archiveItem = Get-Item -LiteralPath $archivePath
if ($archiveItem.Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw 'Refusing to remove source files through a directory link.'
}
$manifest = Get-Content -LiteralPath (Join-Path $bundlePath 'manifest.json') -Raw | ConvertFrom-Json
$retainedNames = @('yelp_academic_dataset_business.json.gz',
                  'yelp_academic_dataset_checkin.json.gz', 'weather_cells.csv',
                  'Dataset_User_Agreement.pdf')
foreach ($name in $retainedNames) {
    $metadata = $manifest.files.PSObject.Properties[$name].Value
    if (!$metadata) { throw "Missing retained file metadata: $name" }
    $retainedFile = Join-Path $bundlePath $name
    $checksum = (Get-FileHash -LiteralPath $retainedFile -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($checksum -ne $metadata.sha256) { throw "Retained file checksum failed: $name" }
}
foreach ($name in @('yelp_academic_dataset_business.json', 'yelp_academic_dataset_checkin.json')) {
    $originalFile = Join-Path $archivePath $name
    if (Test-Path -LiteralPath $originalFile) {
        $checksum = (Get-FileHash -LiteralPath $originalFile -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($checksum -ne $manifest.original_sources.PSObject.Properties[$name].Value.sha256) {
            throw "Original source differs from the verified export: $name"
        }
    }
}
$sourceNames = @('yelp_academic_dataset_business.json', 'yelp_academic_dataset_checkin.json',
                 'yelp_academic_dataset_review.json', 'yelp_academic_dataset_tip.json',
                 'yelp_academic_dataset_user.json', 'weather_cells.csv',
                 'Dataset_User_Agreement.pdf')
$removedBytes = 0L
$pending = @()
foreach ($name in $sourceNames) {
    $sourceFile = Join-Path $resolvedArchive $name
    if (!(Test-Path -LiteralPath $sourceFile)) { continue }
    $resolvedSource = (Resolve-Path -LiteralPath $sourceFile).ProviderPath
    if ((Split-Path -Parent $resolvedSource) -ne $resolvedArchive) {
        throw 'Removal target outside archive.'
    }
    $length = (Get-Item -LiteralPath $resolvedSource).Length
    try {
        Remove-Item -LiteralPath $resolvedSource -ErrorAction Stop
        $removedBytes += $length
    } catch {
        $pending += $name
        Write-Warning "Could not remove $name. Close its editor/viewer and retry."
    }
}
if ((Get-ChildItem -LiteralPath $resolvedArchive -Force | Measure-Object).Count -eq 0) {
    Remove-Item -LiteralPath $resolvedArchive
}
Write-Output "Removed $removedBytes bytes; retained the verified compact bundle and database."
if ($pending.Count -gt 0) { exit 1 }
