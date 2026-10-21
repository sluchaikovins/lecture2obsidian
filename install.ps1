# Лекция → Obsidian: установщик для Windows 10/11 (бета).
# Запуск: правой кнопкой по install.ps1 → «Выполнить с помощью PowerShell»
# или в PowerShell:  powershell -ExecutionPolicy Bypass -File install.ps1
# Всё ставится в папку пользователя, права администратора не нужны (кроме установки Python через winget, если его нет).

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest is 10x slower with the progress bar on
[Console]::OutputEncoding = [Text.Encoding]::UTF8

$Src  = $PSScriptRoot
$Dest = Join-Path $env:USERPROFILE "lecture2obsidian"
$Bin  = Join-Path $Dest "bin"
$Repo = "sluchaikovins/lecture2obsidian"
$WhisperPath = "ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin"
$WhisperSha1 = "4af2b29d7ec73d781377bfd1758ca957a807e941"
$Scripts = "process.py","exam.py","live.py","recap.py","telegram.py","render.py","osutil.py","recorder.py","tray.py","README.md","config.example.json"

function Step($t) { Write-Host ""; Write-Host $t -ForegroundColor Cyan }
function Ok($t)   { Write-Host "  [ok] $t" -ForegroundColor Green }
function Die($t)  { Write-Host ""; Write-Host "  [!] $t" -ForegroundColor Red; Read-Host "Нажмите Enter, чтобы закрыть"; exit 1 }

function Fetch($url, $out) {
    for ($i = 1; $i -le 5; $i++) {
        try { Invoke-WebRequest -Uri $url -OutFile $out -UseBasicParsing; return }
        catch { Write-Host "  Связь оборвалась, повтор $i/5…"; Start-Sleep 3 }
    }
    Die "Не удалось скачать $url. Проверьте интернет (или включите VPN) и запустите установщик ещё раз."
}

function LatestAsset($repo, $pattern, $releases = 10) {
    $rels = Invoke-RestMethod -UseBasicParsing "https://api.github.com/repos/$repo/releases?per_page=$releases"
    foreach ($r in $rels) { foreach ($a in $r.assets) { if ($a.name -match $pattern) { return $a.browser_download_url } } }
    return $null
}

Write-Host "Лекция → Obsidian: установка для Windows (бета)" -ForegroundColor White

# ---------- 0. Проверки ----------
if (-not [Environment]::Is64BitOperatingSystem) { Die "Нужна 64-битная Windows 10 или 11." }
$ramGb  = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)
$freeGb = [math]::Round((Get-PSDrive ($env:USERPROFILE.Substring(0,1))).Free / 1GB)
if ($freeGb -lt 20) { Die "Нужно минимум 20 ГБ свободного места (сейчас $freeGb ГБ)." }
$vramGb = 0
$nvsmi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if ($nvsmi) {
    $mb = (& nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | Select-Object -First 1)
    if ($mb) { $vramGb = [math]::Round([int]$mb / 1024) }
}
Ok "Память $ramGb ГБ, видеокарта NVIDIA: $(if ($vramGb) { "$vramGb ГБ" } else { 'нет' })"
if (-not $vramGb) {
    Write-Host "  Без видеокарты NVIDIA всё работает на процессоре: конспект часовой лекции будет делаться долго (30+ минут)." -ForegroundColor Yellow
}

# ---------- 1. Python ----------
Step "1/7 Python"
# A Python 3.10+ as an array: the program, then its arguments ("py", "-3.12").
function FindPython {
    foreach ($c in @(@("py", "-3.12"), @("py", "-3.11"), @("py", "-3"), @("python"))) {
        $rest = @($c | Select-Object -Skip 1)
        try {
            $v = & $c[0] @rest --version 2>$null
            if ($v -match "Python 3\.(1[0-9])") { return ,$c }
        } catch {}
    }
    return $null
}
$py = FindPython
if (-not $py) {
    Write-Host "  Ставлю Python 3.12…"
    winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements | Out-Null
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + [Environment]::GetEnvironmentVariable("Path", "Machine")
    $py = FindPython
    if (-not $py) { Die "Не получилось поставить Python. Установите Python 3.12 с python.org (галочка «Add to PATH») и запустите установщик снова." }
}
$pyRest = @($py | Select-Object -Skip 1)
Ok "Python: $($py -join ' ')"

# ---------- 2. Файлы программы ----------
Step "2/7 Копирую программу в $Dest"
New-Item -ItemType Directory -Force -Path $Dest, $Bin, (Join-Path $Dest "models"), (Join-Path $Dest "recordings") | Out-Null
if ($Src -ne $Dest) {
    foreach ($f in $Scripts + "install.ps1") { if (Test-Path (Join-Path $Src $f)) { Copy-Item (Join-Path $Src $f) $Dest -Force } }
}
Ok "Готово"

Write-Host "  Окружение Python и библиотеки (звук, картинки, трей, горячие клавиши)…"
$venv = Join-Path $Dest ".venv"
if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
    & $py[0] @pyRest -m venv $venv
}
$vpy  = Join-Path $venv "Scripts\python.exe"
$vpyw = Join-Path $venv "Scripts\pythonw.exe"
& $vpy -m pip install -q --upgrade pip
& $vpy -m pip install -q sounddevice pillow pystray pynput
if ($LASTEXITCODE -ne 0) { Die "Не удалось поставить библиотеки Python." }
Ok "Готово"

# ---------- 3. Распознавание речи: whisper.cpp + ffmpeg ----------
Step "3/7 Распознавание речи (whisper.cpp) и ffmpeg"
if (-not (Test-Path (Join-Path $Bin "whisper-cli.exe"))) {
    $pattern = if ($vramGb) { "^whisper-(bin-win-cuda-12|cublas-12).*x64\.zip$" } else { "^whisper-bin-x64\.zip$" }
    $url = LatestAsset "ggml-org/whisper.cpp" $pattern
    if (-not $url) { $url = LatestAsset "ggml-org/whisper.cpp" "^whisper-bin-x64\.zip$" }
    if (-not $url) { Die "Не нашёл сборку whisper.cpp для Windows на GitHub." }
    $tmp = Join-Path $env:TEMP "l2o-whisper"
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    Fetch $url "$tmp.zip"
    Expand-Archive "$tmp.zip" $tmp -Force
    $cli = Get-ChildItem $tmp -Recurse -Filter "whisper-cli.exe" | Select-Object -First 1
    if (-not $cli) { Die "В архиве whisper.cpp нет whisper-cli.exe." }
    Copy-Item (Join-Path $cli.DirectoryName "*.dll") $Bin -Force
    Copy-Item $cli.FullName $Bin -Force
    Remove-Item -Recurse -Force $tmp, "$tmp.zip"
}
if (-not (Test-Path (Join-Path $Bin "ffmpeg.exe"))) {
    $tmp = Join-Path $env:TEMP "l2o-ffmpeg"
    Fetch "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip" "$tmp.zip"
    Expand-Archive "$tmp.zip" $tmp -Force
    Get-ChildItem $tmp -Recurse -Filter "ffmpeg.exe" | Select-Object -First 1 | Copy-Item -Destination $Bin -Force
    Remove-Item -Recurse -Force $tmp, "$tmp.zip"
}
Ok "Готово"

Step "4/7 Модель распознавания речи Whisper (1,6 ГБ)"
$model = Join-Path $Dest "models\ggml-large-v3-turbo.bin"
function Sha1($f) { if (Test-Path $f) { (Get-FileHash -Algorithm SHA1 $f).Hash.ToLower() } else { "" } }
if ((Sha1 $model) -ne $WhisperSha1) {
    $host_ = "huggingface.co"
    try { Invoke-WebRequest -UseBasicParsing -Method Head "https://huggingface.co" -TimeoutSec 10 | Out-Null } catch { $host_ = "hf-mirror.com" }
    Fetch "https://$host_/$WhisperPath" $model
    if ((Sha1 $model) -ne $WhisperSha1) { Remove-Item $model; Die "Модель Whisper скачалась с ошибкой, запустите установщик ещё раз." }
}
Ok "Готово"

# ---------- 5. Ollama + модели для конспектов ----------
if     ($vramGb -ge 12) { $llm = "qwen3:14b"; $vision = "qwen3-vl:8b" }
elseif ($vramGb -ge 8)  { $llm = "qwen3:8b";  $vision = "qwen3-vl:4b" }
elseif ($ramGb -ge 16)  { $llm = "qwen3:8b";  $vision = "" }
else                    { $llm = "qwen3:4b";  $vision = "" }
Step "5/7 Ollama и модели для конспектов: $llm $(if ($vision) { "и $vision" })"
$ollama = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
if (-not (Test-Path $ollama)) {
    $setup = Join-Path $env:TEMP "OllamaSetup.exe"
    Fetch "https://github.com/ollama/ollama/releases/latest/download/OllamaSetup.exe" $setup
    Write-Host "  Устанавливаю Ollama…"
    Start-Process $setup -ArgumentList "/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES" -Wait
    Remove-Item $setup
}
if (-not (Test-Path $ollama)) { Die "Ollama не установилась. Поставьте её вручную с ollama.com и запустите установщик снова." }
try { Invoke-RestMethod -UseBasicParsing "http://127.0.0.1:11434/api/version" -TimeoutSec 3 | Out-Null }
catch { Start-Process $ollama -ArgumentList "serve" -WindowStyle Hidden; Start-Sleep 5 }
foreach ($m in @($llm, $vision) | Where-Object { $_ }) {
    for ($i = 1; $i -le 5; $i++) { & $ollama pull $m; if ($LASTEXITCODE -eq 0) { break } }
    if ($LASTEXITCODE -ne 0) { Die "Не удалось скачать модель $m. Проверьте интернет (или VPN) и запустите установщик ещё раз." }
}
Ok "Готово"

# ---------- 6. Obsidian и хранилище ----------
Step "6/7 Obsidian и хранилище"
$obsExe = Join-Path $env:LOCALAPPDATA "Programs\Obsidian\Obsidian.exe"
if (-not (Test-Path $obsExe)) {
    $url = LatestAsset "obsidianmd/obsidian-releases" "^Obsidian-[0-9.]+\.exe$" 20
    if ($url) {
        $setup = Join-Path $env:TEMP "ObsidianSetup.exe"
        Fetch $url $setup
        Start-Process $setup -ArgumentList "/S" -Wait
        Remove-Item $setup
    }
}
$obsJson = Join-Path $env:APPDATA "obsidian\obsidian.json"
$vault = $null
if (Test-Path $obsJson) {
    $vaults = @((Get-Content $obsJson -Raw -Encoding UTF8 | ConvertFrom-Json).vaults.PSObject.Properties.Value |
                Where-Object { Test-Path $_.path } | ForEach-Object { $_.path })
    if ($vaults.Count -eq 1) { $vault = $vaults[0] }
    elseif ($vaults.Count -gt 1) {
        Write-Host "  У вас несколько хранилищ Obsidian. Куда складывать лекции?"
        for ($k = 0; $k -lt $vaults.Count; $k++) { Write-Host "    $($k + 1)) $($vaults[$k])" }
        $n = Read-Host "  Номер"
        $vault = $vaults[[Math]::Max(0, [int]$n - 1)]
    }
}
if (-not $vault) {
    $vault = Join-Path ([Environment]::GetFolderPath("MyDocuments")) "Obsidian"
    New-Item -ItemType Directory -Force -Path (Join-Path $vault ".obsidian") | Out-Null
}
$lectures = Join-Path $vault "Лекции"
New-Item -ItemType Directory -Force -Path (Join-Path $lectures "Audio") | Out-Null
Ok "Лекции будут сохраняться в: $lectures"

# Карточки: плагин Spaced Repetition.
$plugin = Join-Path $vault ".obsidian\plugins\obsidian-spaced-repetition"
if (-not (Test-Path (Join-Path $plugin "main.js"))) {
    New-Item -ItemType Directory -Force -Path $plugin | Out-Null
    $rel = Invoke-RestMethod -UseBasicParsing "https://api.github.com/repos/st3v3nmw/obsidian-spaced-repetition/releases/latest"
    foreach ($f in "main.js", "manifest.json", "styles.css") {
        $a = $rel.assets | Where-Object { $_.name -eq $f } | Select-Object -First 1
        if ($a) { Fetch $a.browser_download_url (Join-Path $plugin $f) }
    }
}
$cp = Join-Path $vault ".obsidian\community-plugins.json"
$list = @(); if (Test-Path $cp) { $list = @(Get-Content $cp -Raw | ConvertFrom-Json) }
if ($list -notcontains "obsidian-spaced-repetition") { $list += "obsidian-spaced-repetition" }
[IO.File]::WriteAllText($cp, (ConvertTo-Json @($list)), (New-Object Text.UTF8Encoding $false))  # Obsidian wants no BOM

# ---------- 7. Настройки и автозапуск ----------
Step "7/7 Настройки и запуск"
$cfgPath = Join-Path $Dest "config.json"
$cfg = if (Test-Path $cfgPath) { Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json } else { [pscustomobject]@{} }
$defaults = [ordered]@{ vault = $lectures; model = $llm; lang = "ru"; ollama = $ollama; vision = $(if ($vision) { $vision } else { $null });
                        photo_dirs = @("~/Downloads", "~/Desktop", "~/Pictures/Screenshots"); live_answers = $true }
foreach ($k in $defaults.Keys) { if (-not ($cfg.PSObject.Properties.Name -contains $k)) { $cfg | Add-Member $k $defaults[$k] } }
if (-not $cfg.subjects) {
    Write-Host ""
    Write-Host "  По каким предметам будете записывать лекции? Через запятую, например:"
    Write-Host "  Программирование, Матанализ, Физика, История"
    $line = Read-Host "  Предметы (Enter — пропустить, можно задать потом в меню значка)"
    $cfg | Add-Member subjects @($line -split "[,;]" | ForEach-Object { $_.Trim() } | Where-Object { $_ }) -Force
}
# UTF-8 without BOM: Python's json.loads rejects a BOM.
[IO.File]::WriteAllText($cfgPath, ($cfg | ConvertTo-Json -Depth 5), (New-Object Text.UTF8Encoding $false))

# Autostart: a shortcut in the Startup folder runs the tray app at login.
$lnk = Join-Path ([Environment]::GetFolderPath("Startup")) "Лекция в Obsidian.lnk"
$sh = New-Object -ComObject WScript.Shell
$s = $sh.CreateShortcut($lnk)
$s.TargetPath = $vpyw
$s.Arguments = "`"$(Join-Path $Dest 'tray.py')`""
$s.WorkingDirectory = $Dest
$s.Save()
Start-Process $vpyw -ArgumentList "`"$(Join-Path $Dest 'tray.py')`"" -WorkingDirectory $Dest
Ok "Значок появился в трее (справа внизу, возможно — под стрелкой ^)"

Write-Host ""
Write-Host "Готово! Осталось:" -ForegroundColor Green
Write-Host "  1. Параметры → Конфиденциальность → Микрофон: включите «Разрешить классическим приложениям доступ к микрофону»."
Write-Host "  2. Нажмите F5 (на ноутбуке может быть Fn+F5), скажите пару фраз, F5 ещё раз — через пару минут конспект в Obsidian."
Write-Host "  3. В Obsidian: Настройки → Сторонние плагины → «Включить» — для карточек."
Write-Host "  Во время записи: F7 — что я пропустил, F6 — снимок экрана, Shift+F5 — отменить запись."
Read-Host "Нажмите Enter, чтобы закрыть"
