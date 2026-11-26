-- S1: headless smoke test. Runs 600 frames, logs a few RAM bytes, exits with code 42.
local frames = 0

local function onEndFrame()
  frames = frames + 1
  if frames % 120 == 0 then
    local mode = emu.read(0x0770, emu.memType.nesMemory)
    emu.log(string.format("frame=%d $0770=%02X", frames, mode))
  end
  if frames >= 600 then
    emu.log("S1 done")
    emu.stop(42)
  end
end

emu.addEventCallback(onEndFrame, emu.eventType.endFrame)
emu.log("S1 start")
