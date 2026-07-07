#Requires -Version 5.1
# PrivaVox uninstaller for Windows 10/11 — double-click "Uninstall-PrivaVox.bat".
# Removes: the running app, the runtime at %LOCALAPPDATA%\PrivaVox (code, venv,
# settings, log, dictionary, icons), and the Start Menu + Startup shortcuts.
# Leaves Ollama and uv installed (shared system tools). Optionally removes the
# downloaded AI models (BgGPT via Ollama + the Whisper cache) to reclaim space.
$ErrorActionPreference = 'Stop'
$ProgressPreference    = 'SilentlyContinue'

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

function Write-Step { param($t) Write-Host ''; Write-Host '==> ' -ForegroundColor Cyan -NoNewline; Write-Host $t }
function Write-Ok   { param($t) Write-Host '    ' -NoNewline; Write-Host "OK $t" -ForegroundColor Green }

function Show-Dialog {
    param([string]$Message, [string[]]$Buttons = @('OK'), [switch]$Caution)
    $f = New-Object System.Windows.Forms.Form
    $f.Text = 'PrivaVox — деинсталиране'
    $f.Font = New-Object System.Drawing.Font('Segoe UI', 9.75)
    $f.FormBorderStyle = 'FixedDialog'; $f.MaximizeBox = $false; $f.MinimizeBox = $false
    $f.StartPosition = 'CenterScreen'; $f.TopMost = $true
    $pic = New-Object System.Windows.Forms.PictureBox
    $pic.Size = New-Object System.Drawing.Size(32, 32)
    $pic.Location = New-Object System.Drawing.Point(18, 20)
    if ($Caution) { $pic.Image = [System.Drawing.SystemIcons]::Warning.ToBitmap() }
    else          { $pic.Image = [System.Drawing.SystemIcons]::Information.ToBitmap() }
    $f.Controls.Add($pic)
    $lbl = New-Object System.Windows.Forms.Label
    $lbl.AutoSize = $true; $lbl.MaximumSize = New-Object System.Drawing.Size(400, 0)
    $lbl.Location = New-Object System.Drawing.Point(62, 20); $lbl.Text = $Message
    $f.Controls.Add($lbl)
    $pool = @([System.Windows.Forms.DialogResult]::No, [System.Windows.Forms.DialogResult]::Yes, [System.Windows.Forms.DialogResult]::Retry)
    $btns = @()
    for ($i = 0; $i -lt $Buttons.Count; $i++) {
        $b = New-Object System.Windows.Forms.Button
        $b.Text = $Buttons[$i]; $b.DialogResult = $pool[$i]
        $w = [System.Windows.Forms.TextRenderer]::MeasureText($b.Text, $f.Font).Width + 28
        if ($w -lt 88) { $w = 88 }
        $b.Size = New-Object System.Drawing.Size($w, 27); $btns += $b; $f.Controls.Add($b)
    }
    $f.AcceptButton = $btns[-1]; $f.CancelButton = $btns[0]
    $tw = $lbl.PreferredSize.Width; $th = $lbl.PreferredSize.Height
    $cw = 62 + $tw + 24; if ($cw -lt 360) { $cw = 360 }
    $bh = $th; if ($bh -lt 34) { $bh = 34 }
    $by = 20 + $bh + 18; $x = $cw - 12
    for ($i = $btns.Count - 1; $i -ge 0; $i--) { $x -= $btns[$i].Width; $btns[$i].Location = New-Object System.Drawing.Point($x, $by); $x -= 8 }
    $f.ClientSize = New-Object System.Drawing.Size($cw, ($by + 27 + 14))
    $r = $f.ShowDialog(); $f.Dispose()
    for ($i = 0; $i -lt $Buttons.Count; $i++) { if ($pool[$i] -eq $r) { return $Buttons[$i] } }
    return $Buttons[0]
}

$AppDir = Join-Path $env:LOCALAPPDATA 'PrivaVox'

Write-Host ''
Write-Host '  PrivaVox — деинсталиране' -ForegroundColor Magenta

if ((Show-Dialog -Caution -Message 'Да премахна ли PrivaVox от този компютър?' -Buttons 'Отказ', 'Премахни') -ne 'Премахни') {
    Write-Host 'Отказано.'; exit 0
}

# Прочети избрания модел ПРЕДИ да изтрием настройките (за опцията по-долу).
$model = $null
$settings = Join-Path $AppDir 'settings.json'
if (Test-Path $settings) {
    try { $model = (Get-Content $settings -Raw | ConvertFrom-Json).ollama_model } catch { $model = $null }
}

# --- 1. Спри приложението (само нашия pythonw, не чужди) ---
Write-Step 'Спиране на PrivaVox'
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -match 'flow\.app' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Milliseconds 800
Write-Ok 'Приложението е спряно'

# --- 2. Шорткъти ---
Write-Step 'Премахване на преките пътища'
$lnks = @(
    (Join-Path ([Environment]::GetFolderPath('Programs')) 'PrivaVox.lnk'),
    (Join-Path ([Environment]::GetFolderPath('Startup'))  'PrivaVox.lnk')
)
foreach ($l in $lnks) { if (Test-Path $l) { Remove-Item -Force -ErrorAction SilentlyContinue $l } }
Write-Ok 'Преките пътища са премахнати'

# --- 3. Работната папка (код, venv, настройки, лог, речник) ---
Write-Step 'Премахване на файловете (%LOCALAPPDATA%\PrivaVox)'
if (Test-Path $AppDir) {
    try {
        Remove-Item -Recurse -Force $AppDir
        Write-Ok 'Файловете са премахнати'
    } catch {
        [void](Show-Dialog -Caution -Message ("Част от файловете не се изтриха (може PrivaVox още да върви):`r`n`r`n" + $_.Exception.Message + "`r`n`r`nЗатвори PrivaVox и пусни деинсталатора отново."))
        exit 1
    }
} else {
    Write-Ok 'Няма какво да се трие (папката липсва)'
}

# --- 4. По избор: AI моделите (за да освободиш място) ---
$freed = 'BgGPT (~2.5 GB) + Whisper (~1.6 GB)'
if ((Show-Dialog -Message ("Да премахна ли и свалените AI модели, за да освободя място?`r`n`r`n" + $freed + "`r`n`r`nOllama и uv остават инсталирани.") -Buttons 'Не', 'Да, премахни моделите') -eq 'Да, премахни моделите') {
    Write-Step 'Премахване на AI моделите'
    if ($model -and (Get-Command ollama -ErrorAction SilentlyContinue)) {
        & ollama rm $model 2>$null
        Write-Ok ("BgGPT модел премахнат: {0}" -f $model)
    }
    $hf = Join-Path $env:USERPROFILE '.cache\huggingface'
    if (Test-Path $hf) {
        # само PrivaVox-ките whisper модели, не целия HF кеш на потребителя
        Get-ChildItem $hf -Recurse -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match 'faster-whisper|whisper' } |
            ForEach-Object { Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $_.FullName }
        Write-Ok 'Whisper кешът е премахнат'
    }
}

[void](Show-Dialog -Message ("PrivaVox е премахнат.`r`n`r`nOllama и uv останаха инсталирани — ако не ги ползваш за друго, махни ги от Settings → Apps.") -Buttons 'Готово')
Write-Host ''
Write-Host 'Готово! Този прозорец може да се затвори.' -ForegroundColor Green
