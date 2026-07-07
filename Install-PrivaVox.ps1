#Requires -Version 5.1
# PrivaVox installer for Windows 10/11 — double-click "Install-PrivaVox.bat".
# Mirrors the macOS installer UX: Bulgarian, step markers, one model-choice
# dialog, minimal input. Idempotent: safe to re-run; existing pieces are
# detected and skipped.
#
# NB: PrivaVox is the user-facing product name; the Python package keeps its
# internal codename `flow` (import paths, entry point `-m flow.app`).
#
# Needs: internet. Installs/uses: winget packages (Ollama.Ollama, astral-sh.uv),
# a Python 3.12 venv at %LOCALAPPDATA%\PrivaVox\venv (requirements-runtime-win.txt),
# the chosen BgGPT model (Ollama), the chosen faster-whisper STT model, a Start
# Menu shortcut, optional autostart (shell:startup).
#
# Runtime contract (consumed by flow/* on win32 — W1/W2):
#   entry point        %LOCALAPPDATA%\PrivaVox\venv\Scripts\pythonw.exe -m flow.app
#                      (WorkingDirectory = %LOCALAPPDATA%\PrivaVox)
#   code               %LOCALAPPDATA%\PrivaVox\flow\   (robocopy /MIR from the repo)
#   settings           %LOCALAPPDATA%\PrivaVox\settings.json — merge-written here:
#                      "ollama_model": "<tag>"
#                      "stt_engine":   "faster-whisper-cuda" | "faster-whisper-cpu"
#                      "stt_model":    "deepdml/faster-whisper-large-v3-turbo-ct2" | "small"
#                      (existing keys, e.g. "language_mode"/"hotkey", are preserved)
#   dictionary         %LOCALAPPDATA%\PrivaVox\dictionary.txt (seeded once, never overwritten)
#   log                %LOCALAPPDATA%\PrivaVox\PrivaVox.log (created by the app, not here)
#   Whisper weights    default HuggingFace cache (%USERPROFILE%\.cache\huggingface)
#
# STT hardware detection: nvidia-smi present AND exit 0
#   -> engine "faster-whisper-cuda", model deepdml/faster-whisper-large-v3-turbo-ct2
#   otherwise engine "faster-whisper-cpu" + dialog: Качествен (turbo) / Бърз (small)
#
# Developed/parse-checked on macOS (pwsh + PSScriptAnalyzer). CANNOT be
# validated on macOS — W4 (live Windows validation) must check:
#   - winget IDs install silently (Ollama.Ollama, astral-sh.uv) and the
#     PATH-refresh picks both up without a new console
#   - "ollama app.exe" location, server autostart, /api/version probe timing
#   - uv venv creates Scripts\pythonw.exe; pythonw launches with no console
#   - requirements-runtime-win.txt resolves on Windows (ctranslate2 wheel);
#     CUDA engine additionally needs cuDNN/cuBLAS DLLs at runtime (W2 falls
#     back to CPU if missing — verify)
#   - WinForms dialogs: Bulgarian text, default buttons, DPI scaling
#   - Cyrillic console output under conhost (UTF-8 BOM + chcp 65001 in the .bat)
#   - robocopy /MIR behaviour while PrivaVox is running; re-run of the installer
#     while PrivaVox runs may fail to update venv DLLs (stop it first)
#   - nvidia-smi detection on a real NVIDIA machine; RAM value in the warning
#   - Start Menu + Startup shortcuts render the shipped assets\app-icon.ico;
#     tray shows the shipped color assets\app-icon.png
#   - microphone privacy prompt on first dictation

$ErrorActionPreference = 'Stop'
$ProgressPreference    = 'SilentlyContinue'

# ============================ помощни функции =================================

function Write-Step {
    param([Parameter(Mandatory)][string]$Text)
    Write-Host ''
    Write-Host '==> ' -ForegroundColor Cyan -NoNewline
    Write-Host $Text
}

function Write-Ok {
    param([Parameter(Mandatory)][string]$Text)
    Write-Host '    ✓ ' -ForegroundColor Green -NoNewline
    Write-Host $Text
}

function Test-Tool {
    param([Parameter(Mandatory)][string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Update-SessionPath {
    # winget пише PATH в регистъра; текущият процес не го вижда без рестарт.
    # Добавяме и известните инсталационни папки на ollama/uv за всеки случай.
    $parts = @()
    $parts += [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $parts += [Environment]::GetEnvironmentVariable('Path', 'User')
    $parts += (Join-Path $env:LOCALAPPDATA 'Programs\Ollama')
    $parts += (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links')
    $parts += (Join-Path $env:USERPROFILE '.local\bin')
    $env:Path = (($parts | Where-Object { $_ }) -join ';')
}

# Един WinForms диалог за целия инсталатор (заглавие: "PrivaVox — инсталация"),
# аналог на dialog() от mac инсталатора.
#   Show-FlowDialog [-Caution] -Message "..." -Buttons 'Бутон1','Бутон2'
# Последният бутон е default (Enter). Връща текста на натиснатия бутон;
# затваряне с X/Esc връща първия бутон.
function Show-FlowDialog {
    param(
        [Parameter(Mandatory)][string]$Message,
        [string[]]$Buttons = @('OK'),
        [switch]$Caution
    )
    $form                 = New-Object System.Windows.Forms.Form
    $form.Text            = 'PrivaVox — инсталация'
    $form.Font            = New-Object System.Drawing.Font('Segoe UI', 9.75)
    $form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::FixedDialog
    $form.MaximizeBox     = $false
    $form.MinimizeBox     = $false
    $form.StartPosition   = [System.Windows.Forms.FormStartPosition]::CenterScreen
    $form.TopMost         = $true

    $icon          = New-Object System.Windows.Forms.PictureBox
    $icon.Size     = New-Object System.Drawing.Size(32, 32)
    $icon.Location = New-Object System.Drawing.Point(18, 20)
    if ($Caution) { $icon.Image = [System.Drawing.SystemIcons]::Warning.ToBitmap() }
    else          { $icon.Image = [System.Drawing.SystemIcons]::Information.ToBitmap() }
    $form.Controls.Add($icon)

    $label             = New-Object System.Windows.Forms.Label
    $label.AutoSize    = $true
    $label.MaximumSize = New-Object System.Drawing.Size(400, 0)
    $label.Location    = New-Object System.Drawing.Point(62, 20)
    $label.Text        = $Message
    $form.Controls.Add($label)

    $pool = @([System.Windows.Forms.DialogResult]::Yes,
              [System.Windows.Forms.DialogResult]::No,
              [System.Windows.Forms.DialogResult]::Retry)
    $btns = @()
    for ($i = 0; $i -lt $Buttons.Count; $i++) {
        $b              = New-Object System.Windows.Forms.Button
        $b.Text         = $Buttons[$i]
        $b.DialogResult = $pool[$i]
        $w = [System.Windows.Forms.TextRenderer]::MeasureText($b.Text, $form.Font).Width + 28
        if ($w -lt 88) { $w = 88 }
        $b.Size = New-Object System.Drawing.Size($w, 27)
        $btns += $b
        $form.Controls.Add($b)
    }
    $form.AcceptButton = $btns[-1]   # последният бутон е default, както на mac
    $form.CancelButton = $btns[0]

    $textW   = $label.PreferredSize.Width
    $textH   = $label.PreferredSize.Height
    $btnsW   = 12
    foreach ($b in $btns) { $btnsW += $b.Width + 8 }
    $clientW = 62 + $textW + 24
    if ($clientW -lt $btnsW) { $clientW = $btnsW }
    if ($clientW -lt 360)    { $clientW = 360 }
    $bodyH = $textH
    if ($bodyH -lt 34) { $bodyH = 34 }
    $btnY = 20 + $bodyH + 18
    $x = $clientW - 12
    for ($i = $btns.Count - 1; $i -ge 0; $i--) {
        $x -= $btns[$i].Width
        $btns[$i].Location = New-Object System.Drawing.Point($x, $btnY)
        $x -= 8
    }
    $form.ClientSize = New-Object System.Drawing.Size($clientW, ($btnY + 27 + 14))

    $result = $form.ShowDialog()
    $form.Dispose()
    for ($i = 0; $i -lt $Buttons.Count; $i++) {
        if ($pool[$i] -eq $result) { return $Buttons[$i] }
    }
    return $Buttons[0]   # затворен с X → отказният (първият) бутон
}

# Списъчен диалог, аналог на "choose from list" от mac инсталатора.
# Връща избрания ред или 'cancel'.
function Show-FlowChoice {
    param(
        [Parameter(Mandatory)][string]$Title,
        [Parameter(Mandatory)][string]$Prompt,
        [Parameter(Mandatory)][string[]]$Items,
        [Parameter(Mandatory)][string]$DefaultItem,
        [string]$OkLabel = 'Инсталирай',
        [string]$CancelLabel = 'Отказ'
    )
    $form                 = New-Object System.Windows.Forms.Form
    $form.Text            = $Title
    $form.Font            = New-Object System.Drawing.Font('Segoe UI', 9.75)
    $form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::FixedDialog
    $form.MaximizeBox     = $false
    $form.MinimizeBox     = $false
    $form.StartPosition   = [System.Windows.Forms.FormStartPosition]::CenterScreen
    $form.TopMost         = $true

    $label             = New-Object System.Windows.Forms.Label
    $label.AutoSize    = $true
    $label.MaximumSize = New-Object System.Drawing.Size(560, 0)
    $label.Location    = New-Object System.Drawing.Point(16, 14)
    $label.Text        = $Prompt
    $form.Controls.Add($label)

    $list                = New-Object System.Windows.Forms.ListBox
    $list.IntegralHeight = $false
    $list.Font           = $form.Font
    foreach ($item in $Items) { [void]$list.Items.Add($item) }
    $list.SelectedItem = $DefaultItem
    $form.Controls.Add($list)

    $itemW = 0
    foreach ($item in $Items) {
        $w = [System.Windows.Forms.TextRenderer]::MeasureText($item, $form.Font).Width
        if ($w -gt $itemW) { $itemW = $w }
    }
    $listW = $itemW + 34
    if ($listW -lt 560) { $listW = 560 }
    $labelH = $label.PreferredSize.Height
    $listY  = 14 + $labelH + 10
    $listH  = ($Items.Count * $list.ItemHeight) + 8
    $list.Location = New-Object System.Drawing.Point(16, $listY)
    $list.Size     = New-Object System.Drawing.Size($listW, $listH)

    $btnOk                  = New-Object System.Windows.Forms.Button
    $btnOk.Text             = $OkLabel
    $btnOk.DialogResult     = [System.Windows.Forms.DialogResult]::Yes
    $btnCancel              = New-Object System.Windows.Forms.Button
    $btnCancel.Text         = $CancelLabel
    $btnCancel.DialogResult = [System.Windows.Forms.DialogResult]::No
    foreach ($b in @($btnCancel, $btnOk)) {
        $w = [System.Windows.Forms.TextRenderer]::MeasureText($b.Text, $form.Font).Width + 28
        if ($w -lt 88) { $w = 88 }
        $b.Size = New-Object System.Drawing.Size($w, 27)
        $form.Controls.Add($b)
    }
    $btnY = $listY + $listH + 12
    $btnOk.Location     = New-Object System.Drawing.Point((16 + $listW - $btnOk.Width), $btnY)
    $btnCancel.Location = New-Object System.Drawing.Point((16 + $listW - $btnOk.Width - 8 - $btnCancel.Width), $btnY)
    $form.AcceptButton  = $btnOk
    $form.CancelButton  = $btnCancel
    $list.Add_DoubleClick({ $form.DialogResult = [System.Windows.Forms.DialogResult]::Yes }.GetNewClosure())

    $form.ClientSize = New-Object System.Drawing.Size((16 + $listW + 16), ($btnY + 27 + 14))
    $result   = $form.ShowDialog()
    $selected = $list.SelectedItem
    $form.Dispose()
    if ($result -eq [System.Windows.Forms.DialogResult]::Yes) {
        if ($selected) { return [string]$selected }
        return $DefaultItem
    }
    return 'cancel'
}

# Аналог на fail_dialog() от mac инсталатора.
function Stop-WithDialog {
    param([Parameter(Mandatory)][string]$Message)
    [void](Show-FlowDialog -Caution -Message $Message -Buttons 'OK')
    exit 1
}

function Test-OllamaUp {
    # curl.exe (наличен от Win10 1809+) с --noproxy: системен proxy не бива да
    # пречи на localhost пробата. Fallback: WebRequest с изрично Proxy=$null.
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if ($curl) {
        & curl.exe -s --noproxy '*' --max-time 2 'http://localhost:11434/api/version' *> $null
        return ($LASTEXITCODE -eq 0)
    }
    try {
        $req = [System.Net.WebRequest]::Create('http://localhost:11434/api/version')
        $req.Proxy = $null
        $req.Timeout = 2000
        $resp = $req.GetResponse()
        $resp.Close()
        return $true
    } catch {
        return $false
    }
}

function Wait-OllamaUp {
    param([int]$Seconds)
    for ($i = 0; $i -lt $Seconds; $i++) {
        if (Test-OllamaUp) { return $true }
        if ($i % 5 -eq 4) { Write-Host '    ...чакам Ollama API-то' }
        Start-Sleep -Seconds 1
    }
    return (Test-OllamaUp)
}

# Точен таг: първата колона на `ollama list` == $Tag, като "име" и "име:latest"
# се приравняват в двете посоки (порт на awk проверката от mac инсталатора).
function Test-OllamaHasModel {
    param([Parameter(Mandatory)][string]$Tag)
    $want = $Tag
    if ($want -notmatch ':') { $want = $want + ':latest' }
    try {
        $out = & ollama list 2>$null
    } catch {
        return $false
    }
    if ($LASTEXITCODE -ne 0) { return $false }
    foreach ($line in (@($out) | Select-Object -Skip 1)) {
        if (-not $line) { continue }
        $name = ([string]$line).Trim() -split '\s+'
        $name = $name[0]
        if (-not $name) { continue }
        if ($name -notmatch ':') { $name = $name + ':latest' }
        if ($name -eq $want) { return $true }
    }
    return $false
}

# ============================ инсталация ======================================

trap {
    # никакви тихи крахове: покажи грешката (+ ред и команда) в конзолата и диалог
    $line = $_.InvocationInfo.ScriptLineNumber
    $cmd  = ($_.InvocationInfo.Line).Trim()
    $errText = 'Неочаквана грешка при инсталацията:' + "`r`n`r`n" +
               $_.Exception.Message + "`r`n`r`n" +
               ('(ред {0}: {1})' -f $line, $cmd)
    Write-Host ''
    Write-Host $errText -ForegroundColor Red
    try { [void](Show-FlowDialog -Caution -Message $errText -Buttons 'OK') } catch { $null = $_ }
    exit 1
}

try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { $null = $_ }  # напр. пренасочена конзола
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072  # + TLS 1.2

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$RepoDir = $PSScriptRoot
if (-not $RepoDir) { $RepoDir = (Get-Location).Path }
$AppDir = Join-Path $env:LOCALAPPDATA 'PrivaVox'
# Създаваме работната папка РАНО и я ползваме и за временните .py файлове —
# $env:TEMP може да минава през стар "Local Settings" junction (8.3: LOCAL~1),
# който е недостъпен и чупи Set-Content/robocopy на някои профили.
$null = New-Item -ItemType Directory -Force -Path $AppDir -ErrorAction SilentlyContinue

Write-Host ''
Write-Host '  PrivaVox — локална диктовка (EN/BG)' -ForegroundColor Magenta
Write-Host '  Инсталаторът ще подготви всичко; ще те попита само за AI моделите.' -ForegroundColor Magenta

# --- 0. Система ----------------------------------------------------------------
Write-Step 'Проверка на системата'
$osVer = [Environment]::OSVersion.Version
if ($osVer.Major -lt 10) {
    Stop-WithDialog ('PrivaVox изисква Windows 10 или по-нов.' + "`r`n`r`n" + ('Тази система е с версия {0}.{1}.' -f $osVer.Major, $osVer.Minor))
}
if (-not [Environment]::Is64BitOperatingSystem) {
    Stop-WithDialog 'PrivaVox изисква 64-битов Windows — AI библиотеките нямат 32-битови версии.'
}
if (-not (Test-Path (Join-Path $RepoDir 'flow\app.py'))) {
    Stop-WithDialog ('Инсталаторът трябва да е в папката на PrivaVox.' + "`r`n`r`n" + ('Не намирам flow\app.py в: {0}' -f $RepoDir))
}
Write-Ok ('Windows {0} (64-bit)' -f $osVer.Major)

# --- 1. winget ------------------------------------------------------------------
Write-Step 'Проверка за winget'
if (-not (Test-Tool -Name 'winget')) {
    Stop-WithDialog ('Липсва winget (мениджърът на пакети на Windows).' + "`r`n`r`n" + 'Обнови приложението "App Installer" от Microsoft Store, после пусни инсталатора отново.')
}
Write-Ok 'winget е наличен'

# --- 2. Ollama --------------------------------------------------------------------
Write-Step 'Ollama (локален LLM сървър)'
$ramGB = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB, 1)
if ($ramGB -lt 8) {
    [void](Show-FlowDialog -Caution -Message (('Този компютър има {0} GB RAM, а за AI модела се препоръчват поне 8 GB.' -f $ramGB) + "`r`n`r`n" + 'PrivaVox ще работи, но може да е бавен или нестабилен.') -Buttons 'Продължи')
}
if (-not (Test-Tool -Name 'ollama')) {
    Write-Host '    инсталирам ollama през winget...'
    & winget install --id Ollama.Ollama -e --silent --accept-package-agreements --accept-source-agreements
    Update-SessionPath
    if (-not (Test-Tool -Name 'ollama')) {
        Stop-WithDialog 'Инсталацията на Ollama не успя. Инсталирай го ръчно от https://ollama.com/download/windows и пусни инсталатора отново.'
    }
}
if (-not (Test-OllamaUp)) {
    $ollamaApp = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama app.exe'
    if (Test-Path $ollamaApp) {
        Write-Host '    стартирам приложението Ollama...'
        Start-Process -FilePath $ollamaApp -WindowStyle Hidden
    } else {
        Write-Host '    стартирам ollama serve...'
        Start-Process -FilePath 'ollama' -ArgumentList 'serve' -WindowStyle Hidden
    }
    # Първият старт на Ollama прави onboarding и API-то закъснява — чакаме
    # до 30 s, после пробваме и голия сървър, и пак чакаме.
    if (-not (Wait-OllamaUp -Seconds 30)) {
        Write-Host '    API-то още мълчи — пускам и ollama serve за всеки случай...'
        Start-Process -FilePath 'ollama' -ArgumentList 'serve' -WindowStyle Hidden -ErrorAction SilentlyContinue
        [void](Wait-OllamaUp -Seconds 30)
    }
}
while (-not (Test-OllamaUp)) {
    $btn = Show-FlowDialog -Caution -Buttons @('Отказ', 'Опитай пак') -Message (
        'Ollama още не отговаря на http://localhost:11434.' + "`r`n`r`n" +
        'Ако прозорецът на Ollama се настройва (първо стартиране) — изчакай го да е готов и натисни "Опитай пак".' + "`r`n" +
        'Ако Ollama не е стартиран — пусни го от Start менюто и после "Опитай пак".')
    if ($btn -ne 'Опитай пак') { exit 1 }
    [void](Wait-OllamaUp -Seconds 10)
}
Write-Ok 'Ollama върви'

# --- 3. uv + Python среда ---------------------------------------------------------
Write-Step 'Python среда (uv venv + зависимости)'
if (-not (Test-Tool -Name 'uv')) {
    Write-Host '    инсталирам uv през winget...'
    & winget install --id astral-sh.uv -e --silent --accept-package-agreements --accept-source-agreements
    Update-SessionPath
    if (-not (Test-Tool -Name 'uv')) {
        Stop-WithDialog 'Инсталацията на uv (Python мениджъра) не успя. Пусни инсталатора отново.'
    }
}
New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
$venvDir = Join-Path $AppDir 'venv'
$VenvPy  = Join-Path $venvDir 'Scripts\python.exe'
if (-not (Test-Path $VenvPy)) {
    & uv venv --python 3.12 $venvDir
    if ($LASTEXITCODE -ne 0) { Stop-WithDialog 'Създаването на Python средата не успя (uv venv). Провери интернет връзката и пусни инсталатора отново.' }
    Remove-Item -Force -ErrorAction SilentlyContinue -Path (Join-Path $AppDir '.requirements-runtime-win.txt')
}
$reqSrc  = Join-Path $RepoDir 'requirements-runtime-win.txt'
$reqMark = Join-Path $AppDir '.requirements-runtime-win.txt'
$reqNeed = $true
if (Test-Path $reqMark) {
    if ((Get-Content -Raw -Path $reqSrc) -eq (Get-Content -Raw -Path $reqMark)) { $reqNeed = $false }
}
if ($reqNeed) {
    & uv pip install --quiet --python $VenvPy -r $reqSrc
    if ($LASTEXITCODE -ne 0) { Stop-WithDialog 'Инсталирането на Python зависимостите не успя. Провери интернет връзката и пусни инсталатора отново.' }
    Copy-Item -Force -Path $reqSrc -Destination $reqMark
}
Write-Ok 'Python средата е готова'

# --- 4. Избор на AI модел (всички въпроси — преди дългите сваляния) ----------------
Write-Step 'Избор на AI модел за изчистване на текста'
# какво вече е в Ollama — за да не теглим излишно и за избор на собствен модел
$installed = @()
try { $installed = & ollama list 2>$null | Select-Object -Skip 1 | ForEach-Object { ($_ -split '\s+')[0] } | Where-Object { $_ } } catch {}

$label2tag = [ordered]@{}
$presets = @(
    @{ tag = 'todorov/bggpt:latest';                                         base = 'BgGPT 4B — ПРЕПОРЪЧИТЕЛЕН (баланс качество/скорост)'; size = '2.5 GB' },
    @{ tag = 'todorov/bggpt:Gemma-3-12B-IT-Q4_K_M';                          base = 'BgGPT 12B — по-качествен, ~3x по-бавен';            size = '7.3 GB' },
    @{ tag = 'hf.co/INSAIT-Institute/BgGPT-Gemma-2-2.6B-IT-v1.0-GGUF:Q4_K_M'; base = 'BgGPT 2.6B — най-лек';                              size = '1.7 GB' }
)
foreach ($p in $presets) {
    if (Test-OllamaHasModel -Tag $p.tag) { $label = $p.base + '  —  вече наличен' }
    else                                 { $label = $p.base + '  —  ' + $p.size + ' за сваляне' }
    $label2tag[$label] = $p.tag
}
foreach ($t in $installed) {                       # твои други модели (не-BgGPT) — без сваляне
    if ($t -notmatch 'bggpt') { $label2tag[($t + '  —  твой наличен модел')] = $t }
}
$customLabel = 'Друг модел… (въведи име на Ollama модел ръчно)'
$label2tag[$customLabel] = '__custom__'

$modelItems = @($label2tag.Keys)
$rec = $modelItems[0]
$choice = Show-FlowChoice -Title 'PrivaVox — избор на AI модел' -Prompt 'Кой AI модел да ползва PrivaVox за изчистване на текста? „вече наличен" = без сваляне. Може да смениш и по-късно от менюто в системната лента.' -Items $modelItems -DefaultItem $rec
if ($choice -eq 'cancel') {
    Write-Host 'Отказано от потребителя.'
    exit 0
}
$Model = $label2tag[$choice]
if ($Model -eq '__custom__') {
    Add-Type -AssemblyName Microsoft.VisualBasic
    $Model = [Microsoft.VisualBasic.Interaction]::InputBox('Име на Ollama модел (напр. llama3.1:8b, qwen2.5:7b, todorov/bggpt:latest). Ще бъде свален, ако още го няма. Виж ollama.com/library.', 'PrivaVox — собствен AI модел', '')
    if ([string]::IsNullOrWhiteSpace($Model)) { Write-Host 'Отказано.'; exit 0 }
    $Model = $Model.Trim()
}

# Whisper STT: хардуерна детекция (NVIDIA → GPU; иначе въпрос за CPU режима)
$TurboRepo = 'deepdml/faster-whisper-large-v3-turbo-ct2'
$hasNvidia = $false
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    & nvidia-smi *> $null
    if ($LASTEXITCODE -eq 0) { $hasNvidia = $true }
}
if ($hasNvidia) {
    $SttEngine = 'faster-whisper-cuda'
    $SttModel  = $TurboRepo
    Write-Ok 'Открита е NVIDIA видеокарта — разпознаването на речта ще върви на GPU (turbo)'
} else {
    $SttEngine = 'faster-whisper-cpu'
    $qual = 'Качествен режим (turbo, по-бавен на CPU)  —  ~1.6 GB  —  ПРЕПОРЪЧИТЕЛЕН'
    $fast = 'Бърз режим (по-малък модел small, по-ниско качество)  —  ~0.5 GB'
    $sttChoice = Show-FlowChoice -Title 'PrivaVox — режим на разпознаване на речта' -Prompt 'Няма NVIDIA видеокарта — разпознаването на речта ще върви на процесора. Кой режим предпочиташ?' -Items @($qual, $fast) -DefaultItem $qual
    if ($sttChoice -eq 'cancel') {
        Write-Host 'Отказано от потребителя.'
        exit 0
    }
    if ($sttChoice -like '*Бърз*') { $SttModel = 'small' } else { $SttModel = $TurboRepo }
    Write-Ok ('Разпознаването на речта ще върви на процесора (модел: {0})' -f $SttModel)
}

# сваляне на избрания BgGPT модел (точен таг, както на mac)
if (Test-OllamaHasModel -Tag $Model) {
    Write-Ok ('Моделът {0} вече е изтеглен' -f $Model)
} else {
    Write-Host ('    свалям {0} (прогресът е по-долу)...' -f $Model)
    & ollama pull $Model
    if ($LASTEXITCODE -ne 0) { Stop-WithDialog ('Свалянето на {0} не успя. Провери интернет връзката и пусни инсталатора отново.' -f $Model) }
}
Write-Ok ('AI модел: {0}' -f $Model)

# --- 5. Whisper STT модел -----------------------------------------------------------
$sttSize = '~1.6 GB'
if ($SttModel -eq 'small') { $sttSize = '~0.5 GB' }
Write-Step ('Whisper STT модел ({0}, еднократно)' -f $sttSize)
$prefetchPy = @'
import sys
from faster_whisper import WhisperModel
WhisperModel(sys.argv[1], device="cpu", compute_type="int8")
print("ok")
'@
$tmpPrefetch = Join-Path $AppDir 'privavox-prefetch-whisper.py'
Set-Content -Path $tmpPrefetch -Value $prefetchPy -Encoding ASCII
$env:PYTHONIOENCODING = 'utf-8'
$env:HF_HUB_DISABLE_SYMLINKS_WARNING = '1'
& $VenvPy $tmpPrefetch $SttModel
if ($LASTEXITCODE -eq 0) {
    Write-Ok 'Whisper моделът е наличен'
} else {
    Write-Host '    (!) свалянето на Whisper модела не успя — PrivaVox ще опита отново при първия старт' -ForegroundColor Yellow
}
Remove-Item -Force -ErrorAction SilentlyContinue -Path $tmpPrefetch

# --- 6. Работна среда на приложението -------------------------------------------------
Write-Step 'Инсталиране на работната среда (%LOCALAPPDATA%\PrivaVox)'
& robocopy (Join-Path $RepoDir 'flow') (Join-Path $AppDir 'flow') /MIR /XD __pycache__ /R:2 /W:2 /NFL /NDL /NJH /NJS /NP
if ($LASTEXITCODE -ge 8) { Stop-WithDialog ('Копирането на кода не успя (robocopy код {0}). Спри PrivaVox, ако върви, и пусни инсталатора отново.' -f $LASTEXITCODE) }
# икони: app-icon.png = цветната tray икона; app-icon.ico = Start Menu шорткътът
# (и двете са build артефакти от scripts/make_icons.py, шипнати в repo-то)
Copy-Item -Force -ErrorAction SilentlyContinue -Destination $AppDir -Path @(
    (Join-Path $RepoDir 'assets\menubar-icon.png'),
    (Join-Path $RepoDir 'assets\app-icon.png'),
    (Join-Path $RepoDir 'assets\app-icon.ico')
)
if (-not (Test-Path (Join-Path $AppDir 'dictionary.txt'))) {
    Copy-Item -Path (Join-Path $RepoDir 'dictionary.txt') -Destination $AppDir
}
# запази избраните модели в настройките, без да пипаш език и др. (merge, като на mac)
$provisionPy = @'
import json, sys
path, model, engine, stt_model = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
s = {}
try:
    with open(path, encoding="utf-8") as f:
        s = json.load(f)
except Exception:
    pass
s["ollama_model"] = model
s["stt_engine"] = engine
s["stt_model"] = stt_model
with open(path, "w", encoding="utf-8") as f:
    json.dump(s, f)
'@
$tmpProvision = Join-Path $AppDir 'privavox-provision.py'
Set-Content -Path $tmpProvision -Value $provisionPy -Encoding ASCII
& $VenvPy $tmpProvision (Join-Path $AppDir 'settings.json') $Model $SttEngine $SttModel
if ($LASTEXITCODE -ne 0) { Stop-WithDialog 'Записът на settings.json не успя.' }
Remove-Item -Force -ErrorAction SilentlyContinue -Path $tmpProvision
Write-Host '    settings.json: модел и STT конфигурация записани'
Write-Ok 'Работната среда е готова'

# --- 7. Шорткъти (Start Menu + автостарт по избор) --------------------------------------
# НЕ-фатално: приложението е инсталирано и стартируемо и без шорткът, затова
# всяка грешка тук е предупреждение, не край на инсталацията.
Write-Step 'Шорткът в Start Menu'
$PyW = Join-Path $venvDir 'Scripts\pythonw.exe'
if (-not (Test-Path $PyW)) { $PyW = $VenvPy }   # pythonw.exe → старт без конзола
$lnkPath = $null
try {
    $programs = [Environment]::GetFolderPath('Programs')
    if (-not $programs -or -not (Test-Path $programs)) {
        $programs = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
        $null = New-Item -ItemType Directory -Force -Path $programs -ErrorAction SilentlyContinue
    }
    $lnkPath = Join-Path $programs 'PrivaVox.lnk'
    $wsh = New-Object -ComObject WScript.Shell
    $lnk = $wsh.CreateShortcut($lnkPath)
    $lnk.TargetPath       = $PyW
    $lnk.Arguments        = '-m flow.app'
    $lnk.WorkingDirectory = $AppDir
    $lnk.Description      = 'PrivaVox — локална диктовка (EN/BG)'
    $icoPath = Join-Path $AppDir 'app-icon.ico'
    if (Test-Path $icoPath) { $lnk.IconLocation = $icoPath }
    $lnk.Save()
    Write-Ok ('Шорткътът е създаден: {0}' -f $lnkPath)
} catch {
    Write-Host ('    (!) шорткътът не можа да се създаде ({0}) — PrivaVox пак ще се стартира сега' -f $_.Exception.Message) -ForegroundColor Yellow
    $lnkPath = $null
}

try {
    $answer = Show-FlowDialog -Message 'Да стартира ли PrivaVox автоматично при включване на компютъра?' -Buttons 'Не', 'Да'
    $startupLnk = Join-Path ([Environment]::GetFolderPath('Startup')) 'PrivaVox.lnk'
    if ($answer -eq 'Да' -and $lnkPath -and (Test-Path $lnkPath)) {
        Copy-Item -Force -Path $lnkPath -Destination $startupLnk
        Write-Ok 'Добавен в автостарт (папка Startup)'
    } elseif ($answer -eq 'Да') {
        Write-Host '    (!) автостартът не можа да се зададе (липсва шорткът)' -ForegroundColor Yellow
    } elseif (Test-Path $startupLnk) {
        Remove-Item -Force -Path $startupLnk
        Write-Host '    автостартът е изключен (шорткътът от Startup е премахнат)'
    }
} catch {
    Write-Host ('    (!) автостартът не можа да се зададе ({0})' -f $_.Exception.Message) -ForegroundColor Yellow
}

# --- 8. Старт ------------------------------------------------------------------------
Write-Step 'Стартиране на PrivaVox'
[void](Show-FlowDialog -Message ('Инсталацията завърши! Пускам PrivaVox.' + "`r`n`r`n" + 'Windows може да поиска достъп до микрофона — разреши го.' + "`r`n`r`n" + 'Ползване: задръж дясната Ctrl, говори, пусни я.') -Buttons 'Пусни PrivaVox')
Start-Process -FilePath $PyW -ArgumentList '-m', 'flow.app' -WorkingDirectory $AppDir
Write-Ok 'PrivaVox е стартиран — виж иконата в системната лента (до часовника)'
Write-Host ''
Write-Host 'Готово! Този прозорец може да се затвори.' -ForegroundColor Green
Write-Host ''
exit 0
