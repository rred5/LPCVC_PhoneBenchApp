param(
    [Parameter(Mandatory = $true)]
    [string]$ImagePath
)

$ErrorActionPreference = 'Stop'
$Image = Get-Item -LiteralPath $ImagePath
if ($Image.PSIsContainer) { throw 'ImagePath must point to a file' }
$FileName = $Image.Name
# Keep remote shell arguments unambiguous, including when the PC directory has spaces.
if ($FileName -notmatch '^[A-Za-z0-9_.-]+$') { throw 'Use a filename containing only letters, numbers, dots, underscores, or hyphens' }
$StagingPath = "/data/local/tmp/geniex-$([guid]::NewGuid().ToString('N'))-$FileName"

adb shell run-as com.geniex.demo mkdir -p files/benchmark
if ($LASTEXITCODE -ne 0) { throw 'Cannot access GenieX private files; install the debug app first' }
try {
    adb push $Image.FullName $StagingPath
    if ($LASTEXITCODE -ne 0) { throw 'adb push failed' }
    adb shell run-as com.geniex.demo cp $StagingPath "files/benchmark/$FileName"
    if ($LASTEXITCODE -ne 0) { throw 'Copy into GenieX failed' }
    $RemoteHash = adb shell run-as com.geniex.demo sha256sum "files/benchmark/$FileName"
    if ($LASTEXITCODE -ne 0) { throw 'Could not verify copied image' }
    # Use .NET directly so the transfer works in minimal PowerShell hosts too.
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    $stream = [System.IO.File]::OpenRead($Image.FullName)
    try {
        $LocalHash = [System.BitConverter]::ToString($sha256.ComputeHash($stream)).Replace('-', '').ToLowerInvariant()
    } finally {
        $stream.Dispose()
        $sha256.Dispose()
    }
    if (($RemoteHash -split '\s+')[0] -ne $LocalHash) { throw 'Image checksum mismatch' }
} finally {
    adb shell rm -f $StagingPath
    if ($LASTEXITCODE -ne 0) { Write-Warning "Could not remove $StagingPath" }
}
Write-Host "Verified original bytes: /data/user/0/com.geniex.demo/files/benchmark/$FileName"
