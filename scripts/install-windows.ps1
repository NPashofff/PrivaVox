# PrivaVox — bootstrap инсталатор за Windows (без SmartScreen предупреждение).
#
# Пусни в PowerShell:
#   irm https://github.com/NPashofff/PrivaVox/releases/latest/download/install-windows.ps1 | iex
#
# Стартиран през `iex` (in-process), няма свален .exe/.bat за двоен клик, затова
# SmartScreen „unknown publisher" не се появява. Сваля последното издание,
# премахва Mark-of-the-Web от файловете и пуска истинския инсталатор.
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072

$tmp = Join-Path $env:TEMP ('privavox-boot-' + [guid]::NewGuid().ToString('N').Substring(0,8))
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
$zip = Join-Path $tmp 'PrivaVox-Windows.zip'

Write-Host '==> Сваляне на последната версия…' -ForegroundColor Cyan
Invoke-WebRequest 'https://github.com/NPashofff/PrivaVox/releases/latest/download/PrivaVox-Windows.zip' -OutFile $zip -UseBasicParsing
Write-Host '==> Разархивиране…' -ForegroundColor Cyan
Expand-Archive -Path $zip -DestinationPath $tmp -Force
# махни Mark-of-the-Web, за да не тропа SmartScreen на .bat/.ps1
Get-ChildItem -Recurse $tmp | Unblock-File -ErrorAction SilentlyContinue

$installer = Join-Path $tmp 'PrivaVox-Windows\Install-PrivaVox.ps1'
if (-not (Test-Path $installer)) {
    Write-Host 'Грешка: инсталаторът липсва в архива.' -ForegroundColor Red
    exit 1
}
Write-Host '==> Стартиране на инсталатора…' -ForegroundColor Cyan
& $installer
