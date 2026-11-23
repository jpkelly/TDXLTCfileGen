"""LTC audio encoder, written from ANSI/SMPTE 12M-1986 and SMPTE ST 12-1.

Signal
------
* 80 bits per LTC frame, bit 0 first, biphase mark: every bit starts with a
  transition, a 1 has a second transition half way through.
* Bit 0 of each frame starts exactly at the start of its frame (12M 3.2.2.2;
  the standard allows +/- one video line, we aim for zero).
* The polarity bit (27, or 59 at 25/50 fps) makes the number of zeros in
  every word even. Each frame therefore has an even number of transitions,
  so every frame starts at the same level with a rising edge. The encoder
  relies on that to render any slice of the file independently.
* Edges have a 25 us 10-90% rise time (50 us for EBU 25/50 fps), modelled as
  a first-order RC low-pass sampled at its exact output. Edge times are not
  rounded to samples; each edge is moved earlier by tau*ln2 so its
  half-amplitude point lands exactly on the ideal time (12M 3.3: timings
  are measured at half amplitude).

Output is identical however the file is split into chunks, so audio can be
rendered in one go, in chunks, or a video frame at a time.

Requires numpy (bundled with TouchDesigner).
"""

from __future__ import annotations

import math
import os
import wave
from dataclasses import dataclass

import numpy as np

from .job import Plan

#: Bits 64-79, in transmission order (12M Fig. 3).
SYNC_WORD = np.array([0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 1], dtype=np.uint8)

#: Bit offsets of user-bit groups 1..8.
USER_GROUP_BITS = (4, 12, 20, 28, 36, 44, 52, 60)

LAYOUTS = ("both", "left", "right", "mono")


@dataclass(frozen=True)
class AudioOptions:
    sample_rate: int = 48000
    bit_depth: int = 24              # 16 or 24
    layout: str = "both"             # both | left | right | mono
    level_dbfs: float = -12.0        # peak level of the square wave
    rise_us: float | None = None     # None = standard (25 us, 50 us at 25/50 fps); 0 = hard edges
    user_bits: int = 0               # 32-bit value, group 1 = lowest nibble

    def validate(self) -> None:
        if self.sample_rate not in (44100, 48000, 96000):
            raise ValueError("sample rate must be 44100, 48000 or 96000")
        if self.bit_depth not in (16, 24):
            raise ValueError("bit depth must be 16 or 24")
        if self.layout not in LAYOUTS:
            raise ValueError(f"layout must be one of {LAYOUTS}")
        if not -60.0 <= self.level_dbfs <= 0.0:
            raise ValueError("level must be between -60 and 0 dBFS")
        if self.rise_us is not None and not 0.0 <= self.rise_us <= 200.0:
            raise ValueError("rise time must be between 0 and 200 us")
        if not 0 <= int(self.user_bits) <= 0xFFFFFFFF:
            raise ValueError("user bits must fit in 32 bits")

    @property
    def channels(self) -> int:
        return 1 if self.layout == "mono" else 2

    @property
    def ffmpeg_format(self) -> str:
        return "s16le" if self.bit_depth == 16 else "s24le"

    @property
    def ffmpeg_codec(self) -> str:
        return "pcm_s16le" if self.bit_depth == 16 else "pcm_s24le"


# ----------------------------------------------------------------------
# Frame words


def labels_np(abs_frames: np.ndarray, nominal: int, drop: int):
    """Vectorised timecode.frames_to_label: arrays h, m, s, f."""
    n = np.asarray(abs_frames, dtype=np.int64) % (24 * 6 * (600 * nominal - 9 * drop))
    if drop:
        per_10min = 600 * nominal - 9 * drop
        per_min = 60 * nominal - drop
        tens, rem = np.divmod(n, per_10min)
        n = n + 9 * drop * tens + np.where(rem > drop, drop * ((rem - drop) // per_min), 0)
    f = n % nominal
    s = (n // nominal) % 60
    m = (n // (60 * nominal)) % 60
    h = (n // (3600 * nominal)) % 24
    return h, m, s, f


def frame_bits(h, m, s, f, *, drop_frame: bool, family25: bool, user_bits: int = 0) -> np.ndarray:
    """80-bit LTC words, shape (n, 80), bit 0 first."""
    h, m, s, f = (np.atleast_1d(np.asarray(x, dtype=np.int64)) for x in (h, m, s, f))
    B = np.zeros((len(f), 80), dtype=np.uint8)

    def put(values, start, nbits):
        for k in range(nbits):
            B[:, start + k] = (values >> k) & 1

    put(f % 10, 0, 4)
    put(f // 10, 8, 2)
    B[:, 10] = 1 if drop_frame else 0          # drop-frame flag
    # bit 11 colour-frame flag stays 0
    put(s % 10, 16, 4)
    put(s // 10, 24, 3)
    put(m % 10, 32, 4)
    put(m // 10, 40, 3)
    put(h % 10, 48, 4)
    put(h // 10, 56, 2)
    ub = int(user_bits)
    for g, start in enumerate(USER_GROUP_BITS):
        nib = (ub >> (4 * g)) & 0xF
        for k in range(4):
            B[:, start + k] = (nib >> k) & 1
    B[:, 64:80] = SYNC_WORD
    # Binary group flags (27/43/58/59 minus the polarity bit) stay 0:
    # "no user-bit format specified", time origin arbitrary.
    pol = 59 if family25 else 27
    zeros = 80 - B.sum(axis=1, dtype=np.int64)
    B[:, pol] = (zeros % 2 == 1)               # make the count of zeros even
    assert not np.any((80 - B.sum(axis=1, dtype=np.int64)) % 2), "polarity bit failed"
    return B


# ----------------------------------------------------------------------
# Encoder


class LtcEncoder:
    """Renders the LTC track of a :class:`Plan` as float samples in [-1, 1]."""

    def __init__(self, plan: Plan, opts: AudioOptions = AudioOptions()):
        opts.validate()
        self.plan = plan
        self.opts = opts
        self.sr = opts.sample_rate
        r = plan.rate
        # LTC frame j starts at j * frame_num / frame_den samples (exact ratio).
        self._frame_num = self.sr * r.den * plan.div
        self._frame_den = r.num
        self.frame_len = self._frame_num / self._frame_den
        self.bit_len = self.frame_len / 80.0
        rise = opts.rise_us
        if rise is None:
            rise = 50.0 if r.is_25_family else 25.0
        self.rise_us = rise
        self.tau = rise * 1e-6 * self.sr / math.log(9.0)     # RC time constant, in samples
        self.shift = self.tau * math.log(2.0)               # half-amplitude compensation
        # Residue after `tail` samples is exp(-18) ~ 1.5e-8, below one 24-bit step.
        self.tail = int(math.ceil(18.0 * self.tau)) + 1 if self.tau > 0 else 0
        self.amp = 10.0 ** (opts.level_dbfs / 20.0)
        self.level0 = -self.amp          # level before every frame: bit 0 edge rises
        self.total = plan.total_samples(self.sr)

    def frame_start(self, j: int) -> float:
        """Ideal start of LTC frame j, in samples."""
        return j * self._frame_num / self._frame_den

    def bits(self, j0: int, j1: int) -> np.ndarray:
        """Words for LTC frames j0..j1-1 of the file."""
        p = self.plan
        h, m, s, f = labels_np(p.first_abs + np.arange(j0, j1, dtype=np.int64), p.N, p.D)
        return frame_bits(h, m, s, f, drop_frame=p.drop_frame,
                          family25=p.rate.is_25_family, user_bits=self.opts.user_bits)

    def _edges(self, j0: int, j1: int) -> np.ndarray:
        """Sorted edge times (samples, already shifted) for frames j0..j1-1."""
        if j1 <= j0:
            return np.zeros(0)
        B = self.bits(j0, j1)
        starts = (np.arange(j0, j1, dtype=np.int64) * self._frame_num) / self._frame_den - self.shift
        clock = starts[:, None] + np.arange(80) * self.bit_len
        mid = np.where(B == 1, clock + 0.5 * self.bit_len, np.nan)
        t = np.stack([clock, mid], axis=2).reshape(-1)
        return t[~np.isnan(t)]

    def render(self, a: int, b: int) -> np.ndarray:
        """Samples [a, b) of the track as float64 (clamped to the file)."""
        a = max(0, int(a))
        b = min(self.total, int(b))
        if b <= a:
            return np.zeros(0)
        nframes = self.plan.ltc_frames
        lo = a - self.tail - 1
        j0 = max(0, int(math.floor((lo + self.shift) / self.frame_len)) - 1)
        j1 = min(nframes, int(math.floor((b + self.shift) / self.frame_len)) + 1)
        t = self._edges(j0, j1)
        n = np.arange(a, b, dtype=np.int64)
        # Ideal (unfiltered) level: edges alternate from level0, and every
        # frame holds an even number of edges, so parity of the count is enough.
        cnt = np.searchsorted(t, n, side="right")
        y = np.where(cnt % 2 == 0, self.level0, -self.level0).astype(np.float64)
        if self.tail and len(t):
            k = np.arange(len(t))
            step = np.where(k % 2 == 0, -2.0 * self.level0, 2.0 * self.level0)   # new - old level
            n0 = np.ceil(t).astype(np.int64)
            nn = n0[:, None] + np.arange(self.tail)
            x = nn - t[:, None]
            contrib = -step[:, None] * np.exp(-x / self.tau)
            mask = (nn >= a) & (nn < b)
            y += np.bincount((nn[mask] - a), weights=contrib[mask], minlength=b - a)
        return y

    # ------------------------------------------------------------------
    # PCM output

    def to_pcm(self, y: np.ndarray) -> bytes:
        """Interleaved little-endian PCM bytes for the configured layout."""
        o = self.opts
        full = (1 << (o.bit_depth - 1)) - 1
        mono = np.clip(np.rint(y * full), -full, full).astype("<i4")
        zero = np.zeros_like(mono)
        cols = {"both": (mono, mono), "left": (mono, zero),
                "right": (zero, mono), "mono": (mono,)}[o.layout]
        inter = np.stack(cols, axis=1).reshape(-1)
        if o.bit_depth == 16:
            return inter.astype("<i2").tobytes()
        return inter.view(np.uint8).reshape(-1, 4)[:, :3].tobytes()

    def write_pcm(self, path: str, chunk: int = 1 << 19, progress=None) -> dict:
        """Write the whole track as raw PCM. Returns the format for ffmpeg."""
        with open(path, "wb") as fh:
            for a in range(0, self.total, chunk):
                fh.write(self.to_pcm(self.render(a, a + chunk)))
                if progress:
                    progress(min(a + chunk, self.total) / self.total)
        return {"path": path, "samples": self.total, "sample_rate": self.sr,
                "channels": self.opts.channels, "format": self.opts.ffmpeg_format}

    def write_wav(self, path: str, chunk: int = 1 << 19, progress=None, cancelled=None) -> bool:
        """Write the whole track as a WAV file. False if ``cancelled()`` said stop."""
        check_wav_size(self.total, self.opts)
        with wave.open(path, "wb") as w:
            _wav_format(w, self.opts)
            for a in range(0, self.total, chunk):
                if cancelled and cancelled():
                    return False
                w.writeframesraw(self.to_pcm(self.render(a, a + chunk)))
                if progress:
                    progress(min(a + chunk, self.total) / self.total)
        return True


#: WAV sizes are 32-bit: about 4 h of 48 kHz 24-bit stereo.
WAV_MAX_BYTES = 0xFFFFFFFF - 36


def check_wav_size(samples: int, opts: AudioOptions) -> None:
    size = samples * opts.channels * (opts.bit_depth // 8)
    if size > WAV_MAX_BYTES:
        hours = WAV_MAX_BYTES / (opts.sample_rate * opts.channels * (opts.bit_depth // 8)) / 3600
        raise ValueError(f"a WAV file holds at most {hours:.1f} h at these audio settings "
                         f"(use a shorter duration, 16-bit or mono)")


def _wav_format(w, opts: AudioOptions) -> None:
    w.setnchannels(opts.channels)
    w.setsampwidth(opts.bit_depth // 8)
    w.setframerate(opts.sample_rate)


def pcm_to_wav(pcm_path: str, wav_path: str, opts: AudioOptions, chunk: int = 1 << 22,
               progress=None, cancelled=None) -> bool:
    """Wrap a raw PCM file (as written by :meth:`LtcEncoder.write_pcm`) in a WAV
    header. False if ``cancelled()`` said stop."""
    frame = opts.channels * (opts.bit_depth // 8)
    size = os.path.getsize(pcm_path)
    check_wav_size(size // frame, opts)
    done = 0
    with open(pcm_path, "rb") as src, wave.open(wav_path, "wb") as w:
        _wav_format(w, opts)
        while True:
            if cancelled and cancelled():
                return False
            data = src.read(chunk - chunk % frame)
            if not data:
                break
            w.writeframesraw(data)
            done += len(data)
            if progress:
                progress(done / size if size else 1.0)
    return True
