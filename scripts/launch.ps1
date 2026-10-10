param(
    [string]$ElinGamePath = 'C:\UserData\Steam\steamapps\common\Elin',
    [int]$TimeoutSeconds = 90
)
$ErrorActionPreference = 'Stop'
if ($TimeoutSeconds -lt 1 -or $TimeoutSeconds -gt 300) { throw 'TimeoutSeconds must be between 1 and 300.' }
$executable = (Get-Item -LiteralPath (Join-Path $ElinGamePath 'Elin.exe')).FullName
function Get-InstalledGameProcesses {
    @(Get-Process -Name Elin -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $executable })
}
$existing = Get-InstalledGameProcesses
if ($existing.Count -gt 0) {
    $existing | Select-Object Id,Responding,MainWindowTitle
    Write-Output 'Elin is already running; no new instance was launched.'
    return
}
# Direct game startup can make Steam relaunch it; URI launches can be ignored during shutdown.
$steamProcess = Get-Process -Name steam -ErrorAction SilentlyContinue | Select-Object -First 1
$steamExe = if ($steamProcess) { $steamProcess.Path } else {
    (Get-ItemProperty -LiteralPath 'HKCU:\Software\Valve\Steam' -ErrorAction Stop).SteamExe
}
if (-not $steamExe -or -not (Test-Path -LiteralPath $steamExe)) { throw 'Steam executable not found.' }
Start-Process -FilePath $steamExe -ArgumentList '-applaunch', '2135150'
$deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
do {
    Start-Sleep -Milliseconds 500
    $running = Get-InstalledGameProcesses
    if ($running.Count -gt 1) {
        $running | Select-Object Id,MainWindowTitle
        throw 'Multiple Elin instances appeared. No process was terminated; close the failed duplicate before retrying.'
    }
    if ($running.Count -eq 1 -and $running[0].MainWindowHandle -ne 0) {
        if ($running[0].MainWindowTitle -eq 'Fatal error') { throw 'Steam launched an Elin fatal-error window.' }
        $running | Select-Object Id,Responding,MainWindowTitle
        Write-Output 'Launched Elin through Steam.'
        return
    }
} while ([DateTime]::UtcNow -lt $deadline)
throw 'Elin did not open a game window within the launch timeout. Check Steam; do not launch another EXE instance.'
