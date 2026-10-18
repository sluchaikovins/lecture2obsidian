-- Lecture → Obsidian: F5 starts/stops recording, then the lecture is transcribed and summarized in the background.
-- F6 during recording: screenshot of a screen area (slides) that goes into the lecture note.
-- F7 during recording: «что я пропустил» — recap of the last 10 minutes in a small floating window.
-- Shift+F5: cancel the recording (deleted, no notes).
local HOME = os.getenv("HOME")
local L2O = HOME .. "/lecture2obsidian"
local REC_DIR = L2O .. "/recordings"
local LOG = L2O .. "/l2o.log"
local VAULT = hs.json.read(L2O .. "/config.json").vault

hs.fs.mkdir(REC_DIR)
hs.autoLaunch(true)  -- start with macOS so F5 always works
require("hs.ipc")  -- lets the `hs` CLI talk to this config (debugging)

local recorder, recFile, recStart, tick, listener, recapTimer
local RECAP_MINUTES, AUTO_RECAP_MINUTES = 10, 15
local startAutoRecap  -- defined below the hotkeys; startRecording needs it earlier
local shots = 0
local jobs = 0
L2O_MENU = L2O_MENU or hs.menubar.new()
local menu = L2O_MENU


-- Hammerspoon garbage-collects an hs.task nothing refers to, and that kills the process with it.
-- Every background job is kept here until it finishes.
L2O_TASKS = L2O_TASKS or {}
local function spawn(path, callback, args)
  local task
  task = hs.task.new(path, function(...)
    L2O_TASKS[task] = nil
    if callback then callback(...) end
  end, args)
  L2O_TASKS[task] = true
  task:start()
  return task
end

local function fmt(secs)
  return string.format("%d:%02d:%02d", secs // 3600, secs % 3600 // 60, secs % 60)
end

local function liveEnabled()
  return hs.json.read(L2O .. "/config.json").live_answers ~= false
end

local function refresh()
  local title = "🎓"
  if recorder then
    title = "🔴 " .. fmt(os.time() - recStart)
    if shots > 0 then title = title .. " 📷" .. shots end
  end
  if jobs > 0 then title = title .. " ⏳" .. (jobs > 1 and jobs or "") end
  menu:setTitle(title)
end

local function runJob(script, ...)
  jobs = jobs + 1
  refresh()
  local cmd = string.format('/usr/bin/python3 %q', L2O .. "/" .. script)
  for _, a in ipairs({ ... }) do cmd = cmd .. string.format(" %q", tostring(a)) end
  cmd = cmd .. string.format(" >> %q 2>&1", LOG)
  spawn("/bin/sh", function()
    jobs = jobs - 1
    refresh()
  end, { "-c", cmd })
end

local active = {}  -- recordings being processed right now

local function process(file)
  if active[file] then return end
  active[file] = true
  jobs = jobs + 1
  refresh()
  local cmd = string.format('/usr/bin/python3 %q %q >> %q 2>&1', L2O .. "/process.py", file, LOG)
  spawn("/bin/sh", function()
    active[file] = nil
    jobs = jobs - 1
    refresh()
  end, { "-c", cmd })
end

-- Recordings whose note failed (the audio stays in recordings/ until a note is saved).
local function unfinished()
  local list = {}
  for f in hs.fs.dir(REC_DIR) do
    local path = REC_DIR .. "/" .. f
    if f:match("%.wav$") and (not recorder or path ~= recFile) and not active[path] then table.insert(list, path) end
  end
  return list
end

local function startRecording()
  recFile = REC_DIR .. "/" .. os.date("%Y-%m-%d_%H-%M-%S") .. ".wav"
  recorder = hs.task.new(L2O .. "/bin/recorder", function(code, _, err)
    if code ~= 0 and recorder then
      hs.alert.show("Запись не удалась: " .. (err or code))
      recorder = nil
      if tick then tick:stop() end
      refresh()
    end
  end, { recFile })
  recorder:start()
  recStart = os.time()
  -- Live answers to the lecturer's questions (live.py writes its own log lines).
  if liveEnabled() then
    listener = hs.task.new("/usr/bin/python3", nil, { L2O .. "/live.py", recFile })
    listener:start()
  end
  shots = 0
  tick = hs.timer.doEvery(1, refresh)
  startAutoRecap()
  hs.alert.show("🔴 Запись лекции началась")
  refresh()
end

-- Everything a recording leaves next to it: photos, live transcript, questions, recap page.
local function discard(file)
  local stem = file:gsub("%.wav$", "")
  os.remove(file)
  for _, ext in ipairs({ ".qa.jsonl", ".live.jsonl", ".recap.html", ".transcript.json" }) do os.remove(stem .. ext) end
  hs.execute(string.format("rm -rf %q", stem .. ".photos"))
end

local function stopRecording(cancel)
  local task, file, elapsed = recorder, recFile, os.time() - recStart
  recorder = nil
  tick:stop()
  if listener then listener:terminate(); listener = nil end
  if recapTimer then recapTimer:stop(); recapTimer = nil end
  task:setCallback(function()
    if cancel then
      discard(file)
      hs.alert.show("🗑 Запись отменена и удалена")
    elseif elapsed < 3 then
      discard(file)
      hs.alert.show("Запись отменена (слишком короткая)")
    else
      hs.alert.show("⏹ Запись остановлена (" .. fmt(elapsed) .. "). Делаю конспект…")
      process(file)
    end
    refresh()
  end)
  task:interrupt()  -- SIGINT: recorder finalizes the WAV and exits
end

local function toggle()
  if recorder then stopRecording() else startRecording() end
end

-- The recorder outlives Hammerspoon (crash, reload). Running recorders are adopted on load, so the
-- menu shows 🔴 and F5 stops them normally instead of the recording being stranded.
local function adopt(pid, file)
  local cb
  local obj = {}
  function obj:setCallback(f) cb = f end
  function obj:interrupt()
    hs.execute("kill -INT " .. pid)
    obj.wait = hs.timer.doEvery(0.5, function()
      if not hs.execute("kill -0 " .. pid .. " 2>/dev/null && echo alive"):match("alive") then
        obj.wait:stop()
        if cb then cb(0, "", "") end
      end
    end)
  end
  return obj
end

local function runningRecorders()
  local found = {}
  for pid in hs.execute("pgrep -f '" .. L2O .. "/bin/recorder'"):gmatch("%d+") do
    local file = hs.execute("ps -o args= -p " .. pid):match("bin/recorder%s+(.-)%s*$")
    if file then table.insert(found, { pid = pid, file = file }) end
  end
  return found
end

-- Photos for the current recording live next to it: recordings/<name>.photos/
local function screenshot()
  if not recorder then
    hs.alert.show("Скриншот слайда работает во время записи (F5)")
    return
  end
  local dir = recFile:gsub("%.wav$", ".photos")
  hs.fs.mkdir(dir)
  local path = dir .. "/" .. os.date("%H-%M-%S") .. ".png"
  spawn("/usr/sbin/screencapture", function()
    if hs.fs.attributes(path) then
      shots = shots + 1
      hs.alert.show("📷 Слайд добавлен в конспект")
      refresh()
    end
  end, { "-i", "-x", path })  -- -i: select an area, Esc cancels
end

local function subjects()
  local list = {}
  for f in hs.fs.dir(VAULT) do
    if f ~= "Audio" and f ~= "Фото" and not f:match("^%.") and hs.fs.attributes(VAULT .. "/" .. f, "mode") == "directory" then
      table.insert(list, f)
    end
  end
  table.sort(list)
  return list
end

L2O_HOTKEY = hs.hotkey.bind({}, "f5", toggle)
L2O_SHOT_HOTKEY = hs.hotkey.bind({}, "f6", screenshot)

local function cancelRecording()
  if not recorder then
    hs.alert.show("Сейчас ничего не записывается")
    return
  end
  hs.focus()
  local answer = hs.dialog.blockAlert("Отменить запись?",
    "Запись (" .. fmt(os.time() - recStart) .. ") будет удалена, конспекта не будет.", "Удалить", "Продолжить запись")
  if answer == "Удалить" then stopRecording(true) end
end
L2O_CANCEL_HOTKEY = hs.hotkey.bind({ "shift" }, "f5", cancelRecording)

-- Small floating window in the top-right corner: stays above other windows but doesn't take focus,
-- so typing in the notes app goes on undisturbed.
local function showRecap(html)
  if not L2O_RECAP_VIEW then
    local f = hs.screen.mainScreen():frame()
    local w, h = 420, 520
    L2O_RECAP_VIEW = hs.webview.new({ x = f.x + f.w - w - 16, y = f.y + 16, w = w, h = h })
      :windowStyle({ "titled", "closable", "resizable", "utility", "nonactivating" })
      :windowTitle("👀 Что я пропустил")
      :level(hs.drawing.windowLevels.floating)
      :allowTextEntry(false)
      :deleteOnClose(true)
      :windowCallback(function(action) if action == "closing" then L2O_RECAP_VIEW, L2O_RECAP_PLACED = nil, nil end end)
  end
  L2O_RECAP_VIEW:html(html):show()
  -- Pin to the top-right corner of the screen it's on: macOS may place a new window elsewhere.
  local f, v = hs.screen.mainScreen():frame(), L2O_RECAP_VIEW:frame()
  if v.x + v.w > f.x + f.w or v.x < f.x or v.y < f.y or v.y + v.h > f.y + f.h or not L2O_RECAP_PLACED then
    L2O_RECAP_VIEW:topLeft({ x = f.x + f.w - v.w - 16, y = f.y + 16 })
    L2O_RECAP_PLACED = true
  end
end

local function recap()
  if not recorder then
    hs.alert.show("«Что я пропустил» работает во время записи (F5)")
    return
  end
  local page = recFile:gsub("%.wav$", ".recap.html")
  os.remove(page)
  showRecap("<html><body style='font-family:-apple-system;color:#8a8378;text-align:center;margin-top:80px;"
    .. "background:#fcfaf5'>⏳ Собираю, что было за последние " .. RECAP_MINUTES .. " минут…</body></html>")
  spawn("/usr/bin/python3", function()
    local fh = io.open(page, "r")
    if fh then
      showRecap(fh:read("a"))
      fh:close()
    end
  end, { L2O .. "/recap.py", recFile, tostring(RECAP_MINUTES) })
end
L2O_RECAP_HOTKEY = hs.hotkey.bind({}, "f7", recap)

-- A piece of the notes every AUTO_RECAP_MINUTES while recording.
-- Periodic pieces of the notes go to Telegram, and only if it's set up and "auto_recap" isn't false.
startAutoRecap = function()
  if recapTimer then recapTimer:stop() end
  local cfg = hs.json.read(L2O .. "/config.json")
  if cfg.auto_recap == false or not (cfg.telegram and cfg.telegram.chat_id ~= "" and cfg.telegram.chat_id) then return end
  recapTimer = hs.timer.doEvery(AUTO_RECAP_MINUTES * 60, function()
    if recorder then runJob("recap.py", recFile, AUTO_RECAP_MINUTES, "--auto", "--telegram") end
  end)
end

menu:setMenu(function()
  return {
    { title = recorder and "Остановить запись (F5)" or "Начать запись (F5)", fn = toggle },
    { title = "Скриншот слайда (F6)", fn = screenshot, disabled = not recorder },
    { title = "👀 Что я пропустил (F7)", fn = recap, disabled = not recorder },
    { title = "🗑 Отменить запись (Shift+F5)", fn = cancelRecording, disabled = not recorder },
    { title = "❓ Подсказки на вопросы преподавателя", checked = liveEnabled(), fn = function()
        local cfg = hs.json.read(L2O .. "/config.json")
        cfg.live_answers = not liveEnabled()
        hs.json.write(cfg, L2O .. "/config.json", true, true)
        hs.alert.show(cfg.live_answers and "❓ Подсказки включены (со следующей записи)" or "Подсказки выключены")
      end },
    { title = "-" },
    { title = "📨 Подключить Telegram…", fn = function()
        local cfg = hs.json.read(L2O .. "/config.json")
        hs.focus()
        local button, token = hs.dialog.textPrompt("Telegram-бот",
          "1) В Telegram у @BotFather: /newbot → получите токен.\n2) Откройте своего бота и нажмите Start.\n"
            .. "3) Вставьте токен сюда.",
          (cfg.telegram or {}).token or "", "Подключить", "Отмена")
        if button ~= "Подключить" or token == "" then return end
        cfg.telegram = { token = token:match("^%s*(.-)%s*$"), chat_id = "" }
        hs.json.write(cfg, L2O .. "/config.json", true, true)
        local out, ok = hs.execute(string.format("/usr/bin/python3 %q setup 2>&1", L2O .. "/telegram.py"))
        hs.alert.show(ok and "✅ Telegram подключён — проверьте сообщение от бота" or ("Не вышло: " .. out), 6)
      end },
    { title = "📚 Мои предметы…", fn = function()
        local cfg = hs.json.read(L2O .. "/config.json")
        hs.focus()
        local button, text = hs.dialog.textPrompt("Мои предметы",
          "Перечислите через запятую предметы, по которым пишете лекции. Каждая лекция попадёт в один из них "
            .. "(или в «Разное», если не подходит ни к одному).",
          table.concat(cfg.subjects or {}, ", "), "Сохранить", "Отмена")
        if button ~= "Сохранить" then return end
        local list = {}
        for name in text:gmatch("[^,;\n]+") do
          name = name:match("^%s*(.-)%s*$")
          if name ~= "" then table.insert(list, name) end
        end
        cfg.subjects = list
        hs.json.write(cfg, L2O .. "/config.json", true, true)
        hs.alert.show("📚 Предметов: " .. #list)
      end },
    { title = "📝 Подготовка к экзамену", menu = (function()
        local items = {}
        for _, s in ipairs(subjects()) do
          table.insert(items, { title = s, fn = function()
            hs.alert.show("📝 Готовлю материалы к экзамену: " .. s .. " (несколько минут)")
            runJob("exam.py", s)
          end })
        end
        if #items == 0 then items = { { title = "Пока нет лекций", disabled = true } } end
        return items
      end)() },
    { title = "-" },
    { title = "🔁 Доделать незаконченные записи (" .. #unfinished() .. ")", disabled = #unfinished() == 0, fn = function()
        for _, path in ipairs(unfinished()) do process(path) end
        hs.alert.show("Делаю конспекты по незаконченным записям…")
      end },
    { title = "Открыть папку конспектов", fn = function() hs.execute(string.format("open %q", VAULT)) end },
    { title = "Показать лог", fn = function() hs.execute(string.format("open -a Console %q", LOG)) end },
  }
end)
refresh()

local running = runningRecorders()
for _, r in ipairs(running) do
  local y, mo, d, h, mi, se = r.file:match("(%d+)-(%d+)-(%d+)_(%d+)-(%d+)-(%d+)%.wav$")
  recFile, recorder = r.file, adopt(r.pid, r.file)
  recStart = y and os.time({ year = y, month = mo, day = d, hour = h, min = mi, sec = se }) or os.time()
end
if #running > 0 then
  shots = 0
  tick = hs.timer.doEvery(1, refresh)
  startAutoRecap()
  if liveEnabled() then
    listener = hs.task.new("/usr/bin/python3", nil, { L2O .. "/live.py", recFile })
    listener:start()
  end
  hs.alert.show("🔴 Запись продолжается — F5, чтобы остановить")
  refresh()
else
  -- Recordings left over from a crash or a failed run get another try on startup
  -- (not while a lecture is being recorded: the GPU is busy with live answers then).
  -- Skips files a process.py from before this reload is still working on.
  for f in hs.fs.dir(REC_DIR) do
    local path = REC_DIR .. "/" .. f
    -- "[p]rocess": the pattern must not match the shell running pgrep itself
    local busy = hs.execute(string.format("pgrep -f %q", "[p]rocess.py.*" .. f)) ~= ""
    if f:match("%.wav$") and not busy then process(path) end
  end
end

-- For checking things from the `hs` CLI without pressing keys.
L2O_DEBUG = {
  toggle = toggle,
  show = function(path) local fh = io.open(path, "r"); showRecap(fh:read("a")); fh:close() end,
  cancel = function() if recorder then stopRecording(true) end end,
  recapFor = function(wav)
    spawn("/usr/bin/python3", function()
      local fh = io.open((wav:gsub("%.wav$", ".recap.html")), "r")
      if fh then showRecap(fh:read("a")); fh:close() end
    end, { L2O .. "/recap.py", wav, "1" })
  end,
}
