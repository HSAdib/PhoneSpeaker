# install_apk.ps1 — Helper to install and launch PhoneSpeaker APK on Android

$ErrorActionPreference = "Stop"

# 1. Locate adb
$adb = "adb"
try {
    Get-Command $adb -ErrorAction Stop | Out-Null
} catch {
    $sdkAdb = Join-Path $env:LOCALAPPDATA "Android\Sdk\platform-tools\adb.exe"
    if (Test-Path $sdkAdb) {
        $adb = $sdkAdb
    } else {
        Write-Error "Could not locate adb.exe. Please ensure Android SDK platform-tools are installed."
    }
}

$builtApk = Join-Path $PSScriptRoot "PhoneSpeakerAndroid\app\build\outputs\apk\debug\app-debug.apk"
$apkPath = Join-Path $PSScriptRoot "PhoneSpeaker v2.apk"
if (-not (Test-Path $apkPath) -and (Test-Path (Join-Path $PSScriptRoot "PhoneSpeaker.apk"))) {
    $apkPath = Join-Path $PSScriptRoot "PhoneSpeaker.apk"
}
if (Test-Path $builtApk) {
    Copy-Item -Path $builtApk -Destination (Join-Path $PSScriptRoot "PhoneSpeaker v2.apk") -Force
    $apkPath = Join-Path $PSScriptRoot "PhoneSpeaker v2.apk"
}
if (-not (Test-Path $apkPath)) {
    if (Test-Path $builtApk) {
        $apkPath = $builtApk
    }
}
if (-not (Test-Path $apkPath)) {
    Write-Error "APK not found at $apkPath. Please build it first with: cd PhoneSpeakerAndroid; .\gradlew.bat assembleDebug"
}

Write-Host "Checking for connected Android devices via ADB..." -ForegroundColor Cyan
$devices = & $adb devices | Select-String -Pattern "\b(device|offline|unauthorized)\b"

if (-not $devices) {
    Write-Host "`nNo Android device found via USB or Wireless ADB." -ForegroundColor Yellow
    Write-Host "`nTo connect your phone via Wireless ADB (No cable needed):" -ForegroundColor White
    Write-Host "  1. On your phone: Settings -> Developer options -> Wireless debugging -> ON" -ForegroundColor Gray
    Write-Host "  2. Tap 'Pair device with pairing code'" -ForegroundColor Gray
    Write-Host "  3. In PowerShell run:" -ForegroundColor Gray
    Write-Host "     & `"$adb`" pair <phone_ip>:<pairing_port>" -ForegroundColor White
    Write-Host "     & `"$adb`" connect <phone_ip>:<connect_port>" -ForegroundColor White
    Write-Host "  4. Then re-run this script: .\install_apk.ps1`n" -ForegroundColor White
    exit 1
}

Write-Host "Device detected! Installing $apkPath ..." -ForegroundColor Green
& $adb install -r $apkPath

Write-Host "`nLaunching PhoneSpeaker on device..." -ForegroundColor Green
& $adb shell am start -n com.hasan.phonespeaker.v2/com.hasan.phonespeaker.MainActivity
if ($LASTEXITCODE -ne 0) {
    & $adb shell am start -n com.hasan.phonespeaker/.MainActivity
}

Write-Host "`nDone! PhoneSpeaker is running on your phone." -ForegroundColor Cyan

