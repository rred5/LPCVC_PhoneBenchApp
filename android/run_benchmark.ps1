param(
    [Parameter(Mandatory = $true)]
    [string]$ImagePath,

    [string]$SettingsFile,
    [string]$Model,
    [Nullable[bool]]$ModelAlreadyDownloaded,
    [Nullable[int]]$Repetitions,
    [string[]]$Backends,
    [Nullable[int]]$MaxEdge,
    [Nullable[double]]$RestSeconds,
    [ValidateSet('Cold', 'Warm')]
    [string]$Mode,
    [string]$OutputDirectory,
    [switch]$BuildAndInstall,
    [switch]$SkipPreparation
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
if ([string]::IsNullOrWhiteSpace($SettingsFile)) { $SettingsFile = Join-Path $root 'benchmark_defaults.json' }
if (-not (Test-Path -LiteralPath $SettingsFile)) { throw "Settings file not found: $SettingsFile" }
$defaults = Get-Content -LiteralPath $SettingsFile -Raw | ConvertFrom-Json
if ($defaults.schema_version -ne 1) { throw 'Only settings schema_version 1 is supported' }

# Command-line values override the shared settings file.
if (-not $PSBoundParameters.ContainsKey('Model')) { $Model = $defaults.run.model }
if (-not $PSBoundParameters.ContainsKey('ModelAlreadyDownloaded')) { $ModelAlreadyDownloaded = [bool]$defaults.run.model_already_downloaded }
if (-not $PSBoundParameters.ContainsKey('Repetitions')) { $Repetitions = [int]$defaults.run.repetitions }
if (-not $PSBoundParameters.ContainsKey('Backends')) { $Backends = @($defaults.run.backends) }
if (-not $PSBoundParameters.ContainsKey('MaxEdge')) { $MaxEdge = [int]$defaults.preprocessing.max_edge }
if (-not $PSBoundParameters.ContainsKey('RestSeconds')) { $RestSeconds = [double]$defaults.run.rest_seconds }
if (-not $PSBoundParameters.ContainsKey('Mode')) { $Mode = [string]$defaults.run.mode }

$source = Get-Item -LiteralPath $ImagePath
if ($source.PSIsContainer -or $source.Extension -ine '.png') { throw 'ImagePath must be a PNG file' }
if ($Repetitions -lt 1) { throw 'Repetitions must be positive' }
if ($MaxEdge -lt 1) { throw 'MaxEdge must be positive' }
if ($Mode -notin @('Cold', 'Warm')) { throw 'run.mode must be Cold or Warm' }

if ($BuildAndInstall) {
    $studioJbr = 'C:\Program Files\Android\Android Studio\jbr'
    if ((Test-Path -LiteralPath $studioJbr) -and -not $env:JAVA_HOME) { $env:JAVA_HOME = $studioJbr }
    & "$root\gradlew.bat" assembleDebug assembleDebugAndroidTest --offline
    if ($LASTEXITCODE -ne 0) { throw 'Android build failed' }
    adb install -r "$root\build\outputs\apk\debug\app-debug.apk"
    if ($LASTEXITCODE -ne 0) { throw 'Debug app installation failed' }
    adb install -r "$root\build\outputs\apk\androidTest\debug\app-debug-androidTest.apk"
    if ($LASTEXITCODE -ne 0) { throw 'Instrumentation APK installation failed' }
}

if ($SkipPreparation) {
    $prepared = $source
} else {
    & python "$root\prepare_benchmark_image.py" $source.FullName --max-edge $MaxEdge
    if ($LASTEXITCODE -ne 0) { throw 'Image preparation failed' }
    $prepared = Get-Item -LiteralPath (Join-Path $source.DirectoryName "prepared_$MaxEdge\$($source.BaseName)-$MaxEdge.png")
}

& "$root\push_benchmark_image.ps1" $prepared.FullName
if ($LASTEXITCODE -ne 0) { throw 'Image transfer failed' }

if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $stamp = Get-Date -Format 'yyyy-MM-dd_HHmmss'
    $OutputDirectory = Join-Path $source.DirectoryName "benchmark_$stamp"
}

$runner = if ($Mode -eq 'Warm') { "$root\run_warm_benchmark.py" } else { "$root\run_benchmark_matrix.py" }
$arguments = @(
    $runner,
    '--settings', $SettingsFile,
    '--output', $OutputDirectory,
    '--image', $prepared.Name,
    '--model', $Model,
    $(if ($ModelAlreadyDownloaded) { '--model-already-downloaded' } else { '--no-model-already-downloaded' }),
    '--repetitions', $Repetitions,
    '--backends'
) + $Backends + @('--rest-seconds', $RestSeconds)

Write-Host "Running $Mode benchmark from $SettingsFile."
Write-Host "Results: $OutputDirectory"
& python @arguments
if ($LASTEXITCODE -ne 0) { throw 'Benchmark runner failed' }
