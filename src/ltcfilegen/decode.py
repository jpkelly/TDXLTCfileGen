"""Minimal LTC decoder: zero crossings -> biphase bits -> frames by sync word.

Kept deliberately simple and separate from the encoder. Returns all 80 bits
of every complete frame with its start position (interpolated, in samples),
so flags and the polarity bit can be checked, which ltcdump does not report.
Used by the tests and by post-render verification.
"""

from __future__ import annotations

import numpy as np

SYNC = [0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 1]


def decode(x: np.ndarray, bit_len: float) -> list[tuple[float, list[int]]]:
    """Return [(frame_start_in_samples, bits[80]), ...] for every complete frame."""
    sgn = np.signbit(x)
    idx = np.nonzero(sgn[1:] != sgn[:-1])[0]           # crossing between idx and idx+1
    x0, x1 = x[idx], x[idx + 1]
    t = idx + x0 / (x0 - x1)                            # linear interpolation
    d = np.diff(t)
    bits, starts = [], []
    k = 0
    while k < len(d):
        if d[k] > 0.75 * bit_len:                       # one long interval = 0
            bits.append(0)
            starts.append(t[k])
            k += 1
        elif k + 1 < len(d) and d[k + 1] <= 0.75 * bit_len:   # two short = 1
            bits.append(1)
            starts.append(t[k])
            k += 2
        else:                                           # stray short: resync
            k += 1
    frames = []
    i = 64
    while i + 16 <= len(bits):
        if bits[i:i + 16] == SYNC:
            frames.append((starts[i - 64], bits[i - 64:i + 16]))
            i += 80
        else:
            i += 1
    return frames


def bits_to_label(b: list[int]) -> tuple[int, int, int, int]:
    def val(start, n):
        return sum(b[start + k] << k for k in range(n))

    f = val(0, 4) + 10 * val(8, 2)
    s = val(16, 4) + 10 * val(24, 3)
    m = val(32, 4) + 10 * val(40, 3)
    h = val(48, 4) + 10 * val(56, 2)
    return h, m, s, f
