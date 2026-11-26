-- renes bridge: Mesen (Lua, TCP client) <-> Python orchestrator (TCP server).
-- Line protocol: Python sends one command per line, Lua answers one line ("ok ..." or "err ...").
-- The emulator only advances on "step"; all other commands run while emulation is held.
--
-- Commands:
--   ping                         -> ok pong
--   step <n> [buttons]           -> run n frames holding buttons (e.g. "right+b"); ok frame=<f>
--   reset                        -> ok              (soft reset, like the console's Reset button)
--   read <addr> <len> [memtype]  -> ok <hex>        (default memtype nesDebug: no side effects)
--   write <addr> <hex> [memtype] -> ok
--   save <slot> / load <slot>    -> ok              (performed inside an exec callback, as Mesen requires)
--   savex / loadx <hex>          -> ok <hex> / ok   (savestate transferred to/from Python)
--   cdl                          -> ok <hex>        (PRG-ROM code/data log flags, 1 byte per ROM byte)
--   cdlstat                      -> ok <code> <data> <jumptarget> <subentry>   (counts of flagged bytes)
--   counters <memtype> <type>    -> ok <csv>
--   freeze <addr> <value>        -> ok              (overrides every CPU write to addr)
--   unfreeze <addr>              -> ok
--   pokepc <pc> <addr> <value>   -> ok              (write value to addr each time pc is about to run)
--   pokepcclear                  -> ok
--   apulog                       -> ok              (start recording CPU writes to $4000-$4017; clears)
--   apuwrites                    -> ok frame:addr:value,...   (recorded since apulog)
--   logwrites <start> <end>      -> ok              (record writer PC per address)
--   writers                      -> ok addr:pc:count,...
--   hits <a1,a2,...> [cap] [memtype] / hitlog -> ok / ok addr:f1;f2;...,...  (frames each address
--                                   executes; memtype nesPrgRom watches PRG offsets: one bank's code)
--   trace <start> <end> [max] [memtype] [cpustart] -> ok  (registers per instruction through one
--                                   call; with nesPrgRom, cpustart is the entry's CPU address)
--   tracelog                     -> ok done|open pc:a:x:y:sp:ps:frame,...
--   state                        -> ok key=value ...
--   shot                         -> ok <png hex>
--   quit [code]                  -> stops Mesen

local socket = require("socket.core")

local port = tonumber(os.getenv("RENES_PORT") or "47800")
local conn = socket.tcp()
conn:settimeout(10)
local ok, cerr = conn:connect("127.0.0.1", port)
if not ok then
  emu.stop(91)
  return
end
conn:settimeout(3600)

local frame = 0
local framesLeft = 0
local held = {}
local slots = {}
local frozen = {}
local pcPokes = {}         -- {id, pc} of exec callbacks armed by pokepc
local apuLog, apuLogId = nil, nil   -- "frame:addr:value" for each CPU write to the APU registers
local pendingOp = nil
local writeLog = nil
local writeLogId = nil
local writeLogRange = nil
local hitFrames = nil
local traceLog, traceDone = nil, false
local traceCb = nil        -- {id, start, end} of the armed trace, removed when a new one is armed

local BUTTONS = { "a", "b", "select", "start", "up", "down", "left", "right" }

local function send(line)
  conn:send(line .. "\n")
end

local function tohex(s)
  return (s:gsub(".", function(c) return string.format("%02x", c:byte()) end))
end

-- Mesen returns 0-indexed arrays.
local function arrayhex(arr)
  local parts = {}
  for i = 0, #arr do parts[i + 1] = string.format("%02x", arr[i] & 0xFF) end
  return table.concat(parts)
end

local function arraycsv(arr)
  local parts = {}
  for i = 0, #arr do parts[i + 1] = tostring(arr[i]) end
  return table.concat(parts, ",")
end

local function memtype(name, default)
  return emu.memType[name or default] or emu.memType[default]
end

local held2 = {}   -- controller 2

local function parseButtons(spec)
  local out = {}
  for _, b in ipairs(BUTTONS) do out[b] = false end
  if spec and spec ~= "" and spec ~= "none" then
    for b in spec:gmatch("[^+]+") do out[b] = true end
  end
  return out
end

-- "a+right" holds buttons on controller 1; "a+right/b" also holds B on controller 2.
local function setHeld(spec)
  local p1, p2 = (spec or "none"):match("^([^/]*)/?(.*)$")
  held, held2 = parseButtons(p1), parseButtons(p2)
end

local function splitWords(line)
  local w = {}
  for t in line:gmatch("%S+") do w[#w + 1] = t end
  return w
end

local commandLoop

local function fromhex(hex)
  return (hex:gsub("%x%x", function(b) return string.char(tonumber(b, 16)) end))
end

-- Savestate operations; only legal inside an exec callback.
local function runStateOp(op)
  if op.kind == "save" then
    slots[op.slot] = emu.createSavestate()
    send("ok size=" .. #slots[op.slot])
  elseif op.kind == "load" then
    emu.loadSavestate(slots[op.slot])
    send("ok")
  elseif op.kind == "savex" then
    send("ok " .. tohex(emu.createSavestate()))
  elseif op.kind == "loadx" then
    emu.loadSavestate(fromhex(op.data))
    send("ok")
  end
end

local function onPendingExec()
  local op = pendingOp
  pendingOp = nil
  emu.removeMemoryCallback(op.id, emu.callbackType.exec, 0x0000, 0xFFFF)
  runStateOp(op)
  commandLoop(true)
end

local function scheduleExec(op)
  pendingOp = op
  pendingOp.id = emu.addMemoryCallback(onPendingExec, emu.callbackType.exec, 0x0000, 0xFFFF)
end

-- Blocks on Python commands. Returns when emulation should continue.
-- inExec: true when called from an exec callback (savestates allowed directly).
commandLoop = function(inExec)
  while true do
    local line, rerr = conn:receive("*l")
    if not line then
      emu.stop(92)
      return
    end
    local w = splitWords(line)
    local cmd = w[1]

    if cmd == "ping" then
      send("ok pong")

    elseif cmd == "step" then
      framesLeft = tonumber(w[2]) or 1
      setHeld(w[3])
      return

    elseif cmd == "reset" then
      emu.reset()
      send("ok")

    elseif cmd == "read" then
      local addr, len = tonumber(w[2]), tonumber(w[3])
      local mt = memtype(w[4], "nesDebug")
      local parts = {}
      for i = 0, len - 1 do parts[#parts + 1] = string.format("%02x", emu.read(addr + i, mt)) end
      send("ok " .. table.concat(parts))

    elseif cmd == "write" then
      local addr, hex = tonumber(w[2]), w[3]
      local mt = memtype(w[4], "nesDebug")
      local i = 0
      for byte in hex:gmatch("%x%x") do
        emu.write(addr + i, tonumber(byte, 16), mt)
        i = i + 1
      end
      send("ok")

    elseif cmd == "save" or cmd == "load" or cmd == "savex" or cmd == "loadx" then
      local op = { kind = cmd, slot = w[2] or "0", data = w[2] }
      if cmd == "load" and not slots[op.slot] then
        send("err no-slot")
      elseif inExec then
        runStateOp(op)
      else
        -- Savestates are only legal inside an exec callback: run one instruction, then do it there.
        scheduleExec(op)
        return
      end

    elseif cmd == "cdl" then
      send("ok " .. arrayhex(emu.getCdlData(emu.memType.nesPrgRom)))

    elseif cmd == "cdlstat" then
      -- Counts of PRG bytes flagged code / data / jump target / subroutine entry.
      local cdl = emu.getCdlData(emu.memType.nesPrgRom)
      local code, data, jump, sub = 0, 0, 0, 0
      for i = 0, #cdl do
        local f = cdl[i]
        if f & 1 ~= 0 then code = code + 1 end
        if f & 2 ~= 0 then data = data + 1 end
        if f & 4 ~= 0 then jump = jump + 1 end
        if f & 8 ~= 0 then sub = sub + 1 end
      end
      send(string.format("ok %d %d %d %d", code, data, jump, sub))

    elseif cmd == "counters" then
      local arr = emu.getAccessCounters(memtype(w[2], "nesInternalRam"), emu.counterType[w[3] or "writeCount"])
      send("ok " .. arraycsv(arr))

    elseif cmd == "freeze" then
      local addr, value = tonumber(w[2]), tonumber(w[3])
      if frozen[addr] then emu.removeMemoryCallback(frozen[addr], emu.callbackType.write, addr, addr) end
      frozen[addr] = emu.addMemoryCallback(function() return value end, emu.callbackType.write, addr, addr)
      emu.write(addr, value, emu.memType.nesDebug)
      send("ok")

    elseif cmd == "pokepc" then
      local pc, addr, value = tonumber(w[2]), tonumber(w[3]), tonumber(w[4])
      local id = emu.addMemoryCallback(function() emu.write(addr, value, emu.memType.nesDebug) end,
                                       emu.callbackType.exec, pc, pc)
      pcPokes[#pcPokes + 1] = { id, pc }
      send("ok")

    elseif cmd == "pokepcclear" then
      for _, p in ipairs(pcPokes) do emu.removeMemoryCallback(p[1], emu.callbackType.exec, p[2], p[2]) end
      pcPokes = {}
      send("ok")

    elseif cmd == "apulog" then
      apuLog = {}
      if not apuLogId then
        apuLogId = emu.addMemoryCallback(function(address, value)
          if apuLog then apuLog[#apuLog + 1] = string.format("%d:%04x:%02x", frame, address, value) end
        end, emu.callbackType.write, 0x4000, 0x4017)
      end
      send("ok")

    elseif cmd == "apuwrites" then
      send("ok " .. table.concat(apuLog or {}, ","))

    elseif cmd == "unfreeze" then
      local addr = tonumber(w[2])
      if frozen[addr] then
        emu.removeMemoryCallback(frozen[addr], emu.callbackType.write, addr, addr)
        frozen[addr] = nil
      end
      send("ok")

    elseif cmd == "logwrites" then
      local s, e = tonumber(w[2]), tonumber(w[3])
      if writeLogId then
        emu.removeMemoryCallback(writeLogId, emu.callbackType.write, writeLogRange[1], writeLogRange[2])
      end
      writeLogRange = { s, e }
      writeLog = {}
      writeLogId = emu.addMemoryCallback(function(address)
        local pc = emu.getState()["cpu.pc"]
        local key = address * 65536 + pc
        writeLog[key] = (writeLog[key] or 0) + 1
      end, emu.callbackType.write, s, e)
      send("ok")

    elseif cmd == "writers" then
      local parts = {}
      for key, count in pairs(writeLog or {}) do
        parts[#parts + 1] = string.format("%04x:%04x:%d", key // 65536, key % 65536, count)
      end
      send("ok " .. table.concat(parts, ","))

    elseif cmd == "hits" then
      -- Frames each address executes (once per frame, up to `cap` frames; then the hook is removed).
      -- Adds to the addresses already watched; "hitsclear" starts over.
      hitFrames = hitFrames or {}
      local cap = tonumber(w[3]) or 400
      local mt = memtype(w[4], "nesMemory")
      for a in (w[2] or ""):gmatch("[^,]+") do
        local addr = tonumber(a)
        local list = {}
        hitFrames[addr] = list
        local id
        id = emu.addMemoryCallback(function()
          if list[#list] ~= frame then
            list[#list + 1] = frame
            if #list >= cap then emu.removeMemoryCallback(id, emu.callbackType.exec, addr, addr, emu.cpuType.nes, mt) end
          end
        end, emu.callbackType.exec, addr, addr, emu.cpuType.nes, mt)
      end
      send("ok")

    elseif cmd == "hitsclear" then
      hitFrames = {}
      send("ok")

    elseif cmd == "hitlog" then
      local parts = {}
      for addr, list in pairs(hitFrames or {}) do
        if #list > 0 then parts[#parts + 1] = string.format("%04x:%s", addr, table.concat(list, ";")) end
      end
      send("ok " .. table.concat(parts, ","))

    elseif cmd == "trace" then
      -- Registers before every instruction in [start, end], from the next entry at `start`
      -- until that call returns (an RTS at the entry stack depth) or `max` lines.
      local s, e, max = tonumber(w[2]), tonumber(w[3]), tonumber(w[4]) or 300
      local mt = memtype(w[5], "nesMemory")
      local cs = tonumber(w[6]) or s
      if traceCb then
        emu.removeMemoryCallback(traceCb[1], emu.callbackType.exec, traceCb[2], traceCb[3], emu.cpuType.nes, traceCb[4])
        traceCb = nil
      end
      traceLog, traceDone = {}, false
      local s0 = nil
      local id
      id = emu.addMemoryCallback(function(address)
        if traceDone then return end
        local st = emu.getState()
        if s0 == nil and st["cpu.pc"] ~= cs then return end
        if s0 == nil then s0 = st["cpu.sp"] end
        local pc = st["cpu.pc"]           -- a CPU address even when watching PRG offsets
        traceLog[#traceLog + 1] = string.format("%04x:%02x:%02x:%02x:%02x:%02x:%d", pc, st["cpu.a"],
          st["cpu.x"], st["cpu.y"], st["cpu.sp"], st["cpu.ps"], frame)
        local opcode = emu.read(pc, emu.memType.nesDebug)
        if #traceLog >= max or ((opcode == 0x60 or opcode == 0x40) and st["cpu.sp"] == s0) then
          traceDone = true
          emu.removeMemoryCallback(id, emu.callbackType.exec, s, e, emu.cpuType.nes, mt)
          traceCb = nil
        end
      end, emu.callbackType.exec, s, e, emu.cpuType.nes, mt)
      traceCb = { id, s, e, mt }
      send("ok")

    elseif cmd == "tracelog" then
      send("ok " .. (traceDone and "done " or "open ") .. table.concat(traceLog or {}, ","))

    elseif cmd == "state" then
      local st = emu.getState()
      local parts = { "frame=" .. frame }
      for k, v in pairs(st) do
        if type(v) == "number" and (k:sub(1, 4) == "cpu." or k:sub(1, 4) == "ppu." or k == "frameCount" or k == "masterClock") then
          parts[#parts + 1] = k .. "=" .. v
        elseif type(v) == "boolean" and k:sub(1, 4) == "ppu." then
          parts[#parts + 1] = k .. "=" .. (v and 1 or 0)
        end
      end
      send("ok " .. table.concat(parts, " "))

    elseif cmd == "shot" then
      send("ok " .. tohex(emu.takeScreenshot()))

    elseif cmd == "quit" then
      send("ok")
      emu.stop(tonumber(w[2]) or 0)
      return

    else
      send("err unknown-command " .. tostring(cmd))
    end
  end
end

emu.addEventCallback(function()
  emu.setInput(held, 0, 0)
  -- controller 2 is port 0, sub-port 1: setInput(held2, 1) addressed controller 1 again, so from
  -- 2026-10-21 every frame overwrote player 1 with controller 2's (empty) buttons
  emu.setInput(held2, 0, 1)
end, emu.eventType.inputPolled)

emu.addEventCallback(function()
  frame = frame + 1
  if framesLeft > 0 then
    framesLeft = framesLeft - 1
    if framesLeft > 0 then return end
    send("ok frame=" .. frame)
  end
  commandLoop(false)
end, emu.eventType.endFrame)

setHeld("none")
send("hello mesen frame=0")
