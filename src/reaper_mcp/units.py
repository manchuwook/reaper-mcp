"""Unit conversions shared across the REAPER MCP tool modules.

REAPER stores track, send, and envelope volumes as a *linear amplitude*
factor (the ``D_VOL`` parameter): 1.0 is unity gain, 0.0 is silence, and 2.0
is +6 dB. The tool surface exposed to callers speaks decibels instead, because
that is what the REAPER UI shows and what a musician expects to type. Every
read converts out of linear, every write converts into it.

Keeping these in one module means the conversion cannot drift between the
track, mixing, and mastering tools, which each need it.
"""

import math

#: dB value reported for a linear gain of exactly zero. REAPER's own fader
#: bottoms out around -150 dB, and ``math.log10(0)`` would raise.
SILENCE_DB = -150.0


def gain_to_db(gain: float) -> float:
    """Convert a linear amplitude factor (REAPER ``D_VOL``) to decibels."""
    if gain <= 0:
        return SILENCE_DB
    return 20.0 * math.log10(gain)


def db_to_gain(db: float) -> float:
    """Convert decibels to a linear amplitude factor for REAPER ``D_VOL``."""
    if db <= SILENCE_DB:
        return 0.0
    return 10.0 ** (db / 20.0)
