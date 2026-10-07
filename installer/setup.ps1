<#
  Virtual Room Bot - Windows setup (no administrator rights, no Docker, no Linux tools).

  Downloads a PRIVATE copy of Python, Java and the Lavalink music server into .\runtime (nothing is installed
  system-wide), checks each download against a pinned checksum, installs the bot's Python packages there, then starts
  the guided configuration (bot token, invite link, autostart). Safe to run again: finished steps are skipped.

  Started by double-clicking Setup.cmd. Uninstall: delete the bot folder (and the desktop/startup shortcuts).
#>
param([switch]$RuntimeOnly)   # -RuntimeOnly: install runtimes + packages, skip the interactive part (tests/automation)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'      # much faster downloads in Windows PowerShell 5.1
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$Root = Split-Path -Parent $PSScriptRoot
$Runtime = Join-Path $Root 'runtime'
$Tmp = Join-Path $Runtime '_download'

# Pinned, verified downloads (official sources only).
$Pins = @{
  python   = @{ url = 'https://api.nuget.org/v3-flatcontainer/python/3.12.10/python.3.12.10.nupkg'
                algo = 'SHA512b64'; hash = 'u9pNz2iKlCEbYtUJaKkbOPMF0LjR7NkCafdKhvigpPzrt8oWKgdTpHaR6z3wyWQAm9PYGUxv0Zr66NX9AeHMDw=='
                size = '15 MB'; what = 'Python 3.12.10 (Python Software Foundation)' }
  java     = @{ url = 'https://github.com/adoptium/temurin17-binaries/releases/download/jdk-17.0.20.1%2B1/OpenJDK17U-jre_x64_windows_hotspot_17.0.20.1_1.zip'
                algo = 'SHA256'; hash = 'bc21a93923103cdaac93ee337b0ae4365e739fde36df823dd456bc67c8a9d352'
                size = '44 MB'; what = 'Java 17 runtime (Eclipse Temurin)' }
  lavalink = @{ url = 'https://github.com/lavalink-devs/Lavalink/releases/download/4.2.2/Lavalink.jar'
                algo = 'SHA256'; hash = '8cb801e591072c3689fafd71ccf571a95a4ead3cc35dfc045e157d763d89119a'
                size = '100 MB'; what = 'Lavalink 4.2.2 music server' }
}

function Say([string]$t, [string]$c = 'Gray') { Write-Host $t -ForegroundColor $c }
function Ok([string]$t) { Say "[OK] $t" 'Green' }
function Fail([string]$t) {
  Say "[X]  $t" 'Red'
  Say '     Nothing outside this folder was changed. Fix the problem above and run Setup again.'
  Read-Host 'Press Enter to close'
  exit 1
}

function Get-Verified([string]$name, [string]$dest) {
  $p = $Pins[$name]
  Say "     downloading $($p.what) ($($p.size))..."
  New-Item -ItemType Directory -Force -Path $Tmp | Out-Null
  try { Invoke-WebRequest -Uri $p.url -OutFile $dest -UseBasicParsing }
  catch { Fail "Could not download $($p.what): $($_.Exception.Message). Check the internet connection." }
  if ($p.algo -eq 'SHA512b64') {
    $sha = [System.Security.Cryptography.SHA512]::Create()
    $s = [IO.File]::OpenRead($dest)
    try { $got = [Convert]::ToBase64String($sha.ComputeHash($s)) } finally { $s.Close() }
  } else {
    $got = (Get-FileHash -Algorithm SHA256 -Path $dest).Hash.ToLower()
  }
  if ($got -ne $p.hash) { Remove-Item -Force $dest; Fail "$($p.what) did not match its checksum (download damaged or tampered)." }
  Ok "$($p.what) verified"
}

Say ''
Say 'Virtual Room Bot setup' 'Cyan'
Say '----------------------'
Say 'This puts everything the bot needs inside this folder. No administrator rights needed.'
Say ''

# 1. prerequisites
if (-not [Environment]::Is64BitOperatingSystem) { Fail 'A 64-bit Windows is required.' }
if ([Environment]::OSVersion.Version.Major -lt 10) { Fail 'Windows 10 or 11 is required.' }
$drive = (Get-Item $Root).PSDrive
if ($drive.Free -lt 1.5GB) { Fail "At least 1.5 GB of free disk space is needed on $($drive.Name):." }
if ($Root -match "[^\x20-\x7E]") { Say '     Note: the folder path contains special characters; if setup fails, move the folder to e.g. C:\VirtualRoomBot.' 'Yellow' }
Ok "Windows $([Environment]::OSVersion.Version), 64-bit, enough disk space"

# 2. runtimes (skipped when already present)
New-Item -ItemType Directory -Force -Path $Runtime | Out-Null
$py = Join-Path $Runtime 'python\python.exe'
if (-not (Test-Path $py)) {
  $f = Join-Path $Tmp 'python.zip'
  Get-Verified 'python' $f
  $x = Join-Path $Tmp 'python'
  Expand-Archive -Force -Path $f -DestinationPath $x
  Move-Item -Force (Join-Path $x 'tools') (Join-Path $Runtime 'python')
}
Ok 'Python ready'

$java = Join-Path $Runtime 'java\bin\java.exe'
if (-not (Test-Path $java)) {
  $f = Join-Path $Tmp 'java.zip'
  Get-Verified 'java' $f
  $x = Join-Path $Tmp 'java'
  Expand-Archive -Force -Path $f -DestinationPath $x
  $inner = Get-ChildItem $x -Directory | Select-Object -First 1
  Move-Item -Force $inner.FullName (Join-Path $Runtime 'java')
}
Ok 'Java ready'

$jar = Join-Path $Runtime 'lavalink\Lavalink.jar'
if (-not (Test-Path $jar)) {
  New-Item -ItemType Directory -Force -Path (Join-Path $Runtime 'lavalink') | Out-Null
  $f = Join-Path $Tmp 'Lavalink.jar'
  Get-Verified 'lavalink' $f
  Move-Item -Force $f $jar
}
Ok 'Music server ready'
Remove-Item -Recurse -Force $Tmp -ErrorAction SilentlyContinue

# 3. the bot's Python packages (from PyPI, into the private runtime only)
Say '     installing the bot''s Python packages (about a minute)...'
& $py -m pip --version *> $null
if ($LASTEXITCODE -ne 0) { & $py -m ensurepip --upgrade *> $null }
& $py -m pip install --disable-pip-version-check --no-warn-script-location -q -r (Join-Path $Root 'requirements.txt')
if ($LASTEXITCODE -ne 0) { Fail 'Installing the Python packages failed (see the messages above).' }
Ok 'Bot packages installed'

if ($RuntimeOnly) { Ok 'Runtime installed (-RuntimeOnly: configuration skipped)'; exit 0 }

# 4. guided configuration: token, start, invite link, autostart, desktop shortcut
Say ''
& $py (Join-Path $PSScriptRoot 'vrb.py') configure
$code = $LASTEXITCODE
Say ''
Read-Host 'Press Enter to close'
exit $code
