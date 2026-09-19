# Builds Peak Download Manager for Windows.
#   Output: windows\dist\Peak Download Manager\  (portable folder)
#           windows\Output\PeakSetup.exe          (installer, if Inno Setup is installed)
# Run from any folder:  powershell -ExecutionPolicy Bypass -File windows\build.ps1
$ErrorActionPreference = "Stop"
$Here = $PSScriptRoot
$Root = Split-Path $Here
Set-Location $Here

Write-Host "-> Python packages"
python -m pip install --upgrade -r requirements.txt

# yt-dlp / ffmpeg / aria2c ship inside the app, so users install nothing else.
$Bin = Join-Path $Here "bin"
New-Item -ItemType Directory -Force $Bin | Out-Null
$Tmp = Join-Path $env:TEMP "peak-build"
New-Item -ItemType Directory -Force $Tmp | Out-Null
if (-not (Test-Path "$Bin\yt-dlp.exe")) {
  Write-Host "-> yt-dlp"
  Invoke-WebRequest "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe" -OutFile "$Bin\yt-dlp.exe"
}
if (-not (Test-Path "$Bin\ffmpeg.exe")) {
  Write-Host "-> ffmpeg"
  Invoke-WebRequest "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip" -OutFile "$Tmp\ffmpeg.zip"
  Expand-Archive "$Tmp\ffmpeg.zip" "$Tmp\ffmpeg" -Force
  Get-ChildItem "$Tmp\ffmpeg" -Recurse -Include ffmpeg.exe, ffprobe.exe | Copy-Item -Destination $Bin
}
if (-not (Test-Path "$Bin\aria2c.exe")) {
  Write-Host "-> aria2"
  Invoke-WebRequest "https://github.com/aria2/aria2/releases/download/release-1.37.0/aria2-1.37.0-win-64bit-build1.zip" -OutFile "$Tmp\aria2.zip"
  Expand-Archive "$Tmp\aria2.zip" "$Tmp\aria2" -Force
  Get-ChildItem "$Tmp\aria2" -Recurse -Filter aria2c.exe | Copy-Item -Destination $Bin
}

Write-Host "-> Icon"
python -c "from PIL import Image; Image.open(r'$Root\design\logo\icon1024.png').save(r'$Here\peak.ico', sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])"

Write-Host "-> App"
python -m PyInstaller --noconfirm --clean --windowed `
  --name "Peak Download Manager" --icon peak.ico `
  --paths "$Root\backend" `
  --add-data "$Root\backend\ui;ui" --add-data "$Root\extension;extension" `
  --hidden-import server --hidden-import ytworker --hidden-import ltworker --collect-all libtorrent `
  --collect-submodules yt_dlp `
  --collect-all webview `
  peak_win.py
Copy-Item $Bin "dist\Peak Download Manager\bin" -Recurse -Force

$Iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($Iscc) {
  Write-Host "-> Installer"
  & $Iscc installer.iss
  Write-Host "Done: windows\Output\PeakSetup.exe"
} else {
  Write-Host "Done: windows\dist\Peak Download Manager\Peak Download Manager.exe"
  Write-Host "(Install Inno Setup 6 to also build PeakSetup.exe)"
}
