param([Parameter(Mandatory=$true)][string]$BundleDirectory)
$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$pythonRoot = Join-Path $env:LOCALAPPDATA 'EchoPath\Python313'
$python = Join-Path $pythonRoot 'python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    $installer = Join-Path $BundleDirectory 'python-installer.exe'
    Invoke-WebRequest -UseBasicParsing -Uri 'https://www.python.org/ftp/python/3.13.16/python-3.13.16-amd64.exe' -OutFile $installer
    $hash = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($hash -ne 'fb4f9f5d438b2396da0086dc70b935c530cb578e37adc6d354f7ad2037fee83b') { throw 'Python installer checksum failed.' }
    $signature = Get-AuthenticodeSignature -LiteralPath $installer
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'Python Software Foundation') { throw 'Python installer signature failed.' }
    $process = Start-Process -FilePath $installer -ArgumentList @('/quiet', 'InstallAllUsers=0', 'Include_launcher=0', 'Include_test=0', 'PrependPath=0', ('TargetDir="' + $pythonRoot + '"')) -WindowStyle Hidden -PassThru -Wait
    if ($process.ExitCode -notin @(0,3010)) { throw 'Python installation failed.' }
}
$payload = Join-Path $BundleDirectory 'payload.json'
$installerScript = Join-Path $BundleDirectory 'vm_guest_install.py'
Get-Content -LiteralPath $payload -Raw | & $python $installerScript
if ($LASTEXITCODE -ne 0) { throw 'EchoPath guest bridge installation failed.' }
Write-Output 'EchoPath installed. Keep the Windows desktop signed in and unlocked.'
