param(
    [string]$ElinGamePath = 'C:\UserData\Steam\steamapps\common\Elin',
    [string]$GameVersion = '0.23.352',
    [switch]$Install
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$project = Join-Path $root 'src\ElinGPT.Mod\ElinGPT.Mod.csproj'
if ($GameVersion -notmatch '^\d+\.\d+\.\d+$') { throw 'GameVersion must be major.minor.batch, e.g. 0.23.352.' }
if (-not (Test-Path -LiteralPath (Join-Path $ElinGamePath 'Elin_Data\Managed\Elin.dll'))) {
    throw "Elin.dll not found under $ElinGamePath"
}
& dotnet build $project -c Release "-p:ElinGamePath=$ElinGamePath" --nologo -v minimal
if ($LASTEXITCODE -ne 0) { throw 'Mod build failed.' }
$output = Join-Path $root 'dist\Mod_ElinGPT'
New-Item -ItemType Directory -Path $output -Force | Out-Null
$binary = Join-Path $root 'src\ElinGPT.Mod\bin\Release\net472'
foreach ($name in @('ElinGPT.Mod.dll', 'ElinGPT.Core.dll')) {
    Copy-Item -LiteralPath (Join-Path $binary $name) -Destination (Join-Path $output $name) -Force
}
[xml]$meta = Get-Content -LiteralPath (Join-Path $root 'src\ElinGPT.Mod\package\package.xml')
$meta.Meta.version = $GameVersion
$meta.Save((Join-Path $output 'package.xml'))
Add-Type -AssemblyName System.Drawing
$image = New-Object System.Drawing.Bitmap 512, 512
$graphics = [System.Drawing.Graphics]::FromImage($image)
$font = New-Object System.Drawing.Font 'Segoe UI', 52, ([System.Drawing.FontStyle]::Bold)
$subtitleFont = New-Object System.Drawing.Font 'Segoe UI', 18
$brush = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(225, 235, 242))
try {
    $graphics.Clear([System.Drawing.Color]::FromArgb(22, 34, 43))
    $graphics.DrawString('ElinGPT', $font, $brush, 62, 180)
    $graphics.DrawString('Local agent bridge', $subtitleFont, $brush, 118, 275)
    $image.Save((Join-Path $output 'preview.jpg'), [System.Drawing.Imaging.ImageFormat]::Jpeg)
}
finally {
    $brush.Dispose()
    $subtitleFont.Dispose()
    $font.Dispose()
    $graphics.Dispose()
    $image.Dispose()
}
if ($Install) {
    if (Get-Process | Where-Object { $_.ProcessName -eq 'Elin' }) {
        throw 'Close Elin before installing the mod. No running process will be changed.'
    }
    $destination = Join-Path $ElinGamePath 'Package\Mod_ElinGPT'
    New-Item -ItemType Directory -Path $destination -Force | Out-Null
    foreach ($name in @('ElinGPT.Mod.dll', 'ElinGPT.Core.dll', 'package.xml', 'preview.jpg')) {
        Copy-Item -LiteralPath (Join-Path $output $name) -Destination (Join-Path $destination $name) -Force
    }
    Write-Output "Installed local Workshop package: $destination"
}
else {
    Write-Output "Built Workshop package: $output (not installed)"
}
