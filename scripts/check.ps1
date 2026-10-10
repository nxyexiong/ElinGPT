param([string]$ElinGamePath = 'C:\UserData\Steam\steamapps\common\Elin')
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
& dotnet build (Join-Path $root 'src\ElinGPT.Mod\ElinGPT.Mod.csproj') -c Release "-p:ElinGamePath=$ElinGamePath" --nologo -v minimal
if ($LASTEXITCODE -ne 0) { throw 'Mod compilation failed.' }
& dotnet run --project (Join-Path $root 'tests\ElinGPT.Core.Tests\ElinGPT.Core.Tests.csproj') -c Release "-p:ElinGamePath=$ElinGamePath"
if ($LASTEXITCODE -ne 0) { throw 'Core checks failed.' }
$env:PYTHONPATH = Join-Path $root 'src'
& python -m unittest discover -s (Join-Path $root 'tests\python') -v
if ($LASTEXITCODE -ne 0) { throw 'Python/integration checks failed.' }
