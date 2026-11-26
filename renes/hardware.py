"""Built-in knowledge: NES hardware registers and interrupt vectors (status 'builtin')."""

# (address, name, comment)
REGISTERS: list[tuple[int, str, str]] = [
    (0x2000, "PPUCTRL", "PPU control: NMI enable, sprite size, pattern tables, nametable"),
    (0x2001, "PPUMASK", "PPU mask: rendering enables, color emphasis"),
    (0x2002, "PPUSTATUS", "PPU status: vblank, sprite 0 hit, overflow (read clears latch)"),
    (0x2003, "OAMADDR", "OAM address"),
    (0x2004, "OAMDATA", "OAM data"),
    (0x2005, "PPUSCROLL", "PPU scroll (write x2)"),
    (0x2006, "PPUADDR", "PPU VRAM address (write x2)"),
    (0x2007, "PPUDATA", "PPU VRAM data"),
    (0x4000, "SQ1_VOL", "APU pulse 1 duty/volume"),
    (0x4001, "SQ1_SWEEP", "APU pulse 1 sweep"),
    (0x4002, "SQ1_LO", "APU pulse 1 timer low"),
    (0x4003, "SQ1_HI", "APU pulse 1 length/timer high"),
    (0x4004, "SQ2_VOL", "APU pulse 2 duty/volume"),
    (0x4005, "SQ2_SWEEP", "APU pulse 2 sweep"),
    (0x4006, "SQ2_LO", "APU pulse 2 timer low"),
    (0x4007, "SQ2_HI", "APU pulse 2 length/timer high"),
    (0x4008, "TRI_LINEAR", "APU triangle linear counter"),
    (0x400A, "TRI_LO", "APU triangle timer low"),
    (0x400B, "TRI_HI", "APU triangle length/timer high"),
    (0x400C, "NOISE_VOL", "APU noise volume"),
    (0x400E, "NOISE_LO", "APU noise mode/period"),
    (0x400F, "NOISE_HI", "APU noise length"),
    (0x4010, "DMC_FREQ", "APU DMC flags/rate"),
    (0x4011, "DMC_RAW", "APU DMC direct load"),
    (0x4012, "DMC_START", "APU DMC sample address"),
    (0x4013, "DMC_LEN", "APU DMC sample length"),
    (0x4014, "OAMDMA", "Sprite DMA: copies page $xx00-$xxFF to OAM"),
    (0x4015, "SND_CHN", "APU channel enable / status"),
    (0x4016, "JOY1", "Controller 1 data / strobe"),
    (0x4017, "JOY2_FRAME", "Controller 2 data (read) / APU frame counter (write)"),
]

VECTORS: list[tuple[int, str]] = [(0xFFFA, "NMI"), (0xFFFC, "Reset"), (0xFFFE, "IRQ")]
