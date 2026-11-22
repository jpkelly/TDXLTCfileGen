"""WAV helpers for tests; the decoder lives in ltcfilegen.decode."""

from __future__ import annotations

import wave

import numpy as np

from ltcfilegen.decode import SYNC, bits_to_label, decode  # noqa: F401


def read_wav(path: str, channel: int = 0) -> tuple[np.ndarray, int]:
    with wave.open(path, "rb") as w:
        sr, ch, width, n = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
        raw = w.readframes(n)
    if width == 2:
        x = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32767.0
    elif width == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        v = np.where(v >= 1 << 23, v - (1 << 24), v)
        x = v.astype(np.float64) / 8388607.0
    else:
        raise ValueError(f"unsupported sample width {width}")
    return x.reshape(-1, ch)[:, channel], sr


def write_wav(path: str, pcm: bytes, sr: int, channels: int, bit_depth: int) -> None:
    with wave.open(path, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(bit_depth // 8)
        w.setframerate(sr)
        w.writeframes(pcm)
