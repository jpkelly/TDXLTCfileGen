import os
import re
import shutil
import subprocess

import numpy as np
import pytest

from ltcfilegen import timecode as tc
from ltcfilegen.job import JobSpec, Plan
from ltcfilegen.ltc import AudioOptions, LtcEncoder, frame_bits

from ltc_decode import bits_to_label, decode, read_wav, write_wav

HAVE_LTC_TOOLS = shutil.which("ltcdump") and shutil.which("ltcgen")
needs_ltc_tools = pytest.mark.skipif(not HAVE_LTC_TOOLS, reason="ltc-tools not installed")

# (rate key, drop) for every supported combination
ALL_RATES = [(k, False) for k in tc.RATES] + [("29.97", True), ("59.94", True)]


def _start(drop):
    return "00:59:58;00" if drop else "00:59:58:00"


def _dur(drop):
    return "00:00:04;00" if drop else "00:00:04:00"


# ----------------------------------------------------------------------
# Words


def test_word_layout_and_parity():
    rng = np.random.default_rng(1)
    n = 5000
    h, m, s, f = rng.integers(0, 24, n), rng.integers(0, 60, n), rng.integers(0, 60, n), rng.integers(0, 30, n)
    for fam25 in (False, True):
        for df in (False, True):
            B = frame_bits(h, m, s, f, drop_frame=df, family25=fam25, user_bits=0xDEADBEEF)
            assert np.all((80 - B.sum(axis=1)) % 2 == 0)                # even zeros
            assert np.all(B[:, 64:80] == [0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 1])
            assert np.all(B[:, 10] == int(df))
            assert np.all(B[:, 11] == 0)
            other = 27 if fam25 else 59                              # the non-polarity flag
            assert np.all(B[:, other] == 0) and np.all(B[:, 43] == 0) and np.all(B[:, 58] == 0)
            for k in range(0, n, 997):
                assert bits_to_label(list(B[k])) == (h[k], m[k], s[k], f[k])


# ----------------------------------------------------------------------
# Rendering


@pytest.mark.parametrize("key,drop", ALL_RATES)
def test_chunking_does_not_change_output(key, drop):
    p = Plan(JobSpec(rate=key, drop_frame=drop, start=_start(drop), duration=_dur(drop)))
    e = LtcEncoder(p)
    whole = e.render(0, e.total)
    assert len(whole) == e.total
    for size in (1, 7, 997, 4096):
        if size == 1:
            parts = np.array([e.render(a, a + 1)[0] for a in range(0, 3000)])
            assert np.array_equal(parts, whole[:3000])
            continue
        parts = np.concatenate([e.render(a, a + size) for a in range(0, e.total, size)])
        assert np.array_equal(parts, whole)
    # per video frame, as the render loop will call it
    per_frame = np.concatenate([e.render(*p.sample_range(i, 48000)) for i in range(p.video_frames)])
    assert np.array_equal(per_frame, whole)


@pytest.mark.parametrize("key", ["24", "25", "30"])
def test_mid_level_at_every_frame_start(key):
    """At 24/25/30 fps and 48 kHz frames start on whole samples: half amplitude there."""
    p = Plan(JobSpec(rate=key, duration="00:00:05:00"))
    e = LtcEncoder(p)
    y = e.render(0, e.total)
    starts = [int(e.frame_start(j)) for j in range(p.ltc_frames)]
    assert all(e.frame_start(j) == starts[j] for j in range(p.ltc_frames))
    assert np.max(np.abs(y[starts])) < 1e-4 * e.amp


def test_level_and_no_overshoot():
    p = Plan(JobSpec(rate="25", duration="00:00:02:00"))
    for db in (-6.0, -12.0, -18.0):
        e = LtcEncoder(p, AudioOptions(level_dbfs=db))
        y = e.render(0, e.total)
        amp = 10 ** (db / 20)
        assert np.isclose(y.max(), amp, rtol=1e-9) and np.isclose(y.min(), -amp, rtol=1e-9)


def test_rise_time_option():
    p = Plan(JobSpec(rate="30", duration="00:00:01:00"))
    assert LtcEncoder(p).rise_us == 25.0
    assert LtcEncoder(Plan(JobSpec(rate="25"))).rise_us == 50.0
    hard = LtcEncoder(p, AudioOptions(rise_us=0)).render(0, 4800)
    assert set(np.round(np.unique(hard), 9)) <= {round(-10 ** (-12 / 20), 9), round(10 ** (-12 / 20), 9)}


@pytest.mark.parametrize("key,drop", ALL_RATES)
def test_own_decoder_reads_every_bit(key, drop):
    p = Plan(JobSpec(rate=key, drop_frame=drop, start=_start(drop), duration=_dur(drop)))
    e = LtcEncoder(p, AudioOptions(user_bits=0x13572468))
    y = e.render(0, e.total)
    frames = decode(y, e.bit_len)
    # The last frame can't be decoded by anyone (its bit 79 ends at the next
    # frame's start edge, which is not in the file). This simple decoder also
    # misses frame 0, whose first edge is at the very first sample.
    want = e.bits(0, p.ltc_frames)
    got = {}
    for start, bits in frames:
        j = int(round(start / e.frame_len))
        assert abs(start - e.frame_start(j)) < 0.6                 # interpolated crossing
        got[j] = bits
    assert set(got) == set(range(1, p.ltc_frames - 1))
    for j, bits in got.items():
        assert bits == list(want[j]), f"frame {j}"


@pytest.mark.parametrize("layout,depth", [("both", 24), ("left", 24), ("right", 16), ("mono", 16)])
def test_pcm_layouts(layout, depth, tmp_path):
    p = Plan(JobSpec(rate="25", duration="00:00:01:00"))
    e = LtcEncoder(p, AudioOptions(layout=layout, bit_depth=depth))
    path = str(tmp_path / "a.wav")
    write_wav(path, e.to_pcm(e.render(0, e.total)), 48000, e.opts.channels, depth)
    ref = e.render(0, e.total)
    for ch in range(e.opts.channels):
        x, _ = read_wav(path, ch)
        silent = (layout == "left" and ch == 1) or (layout == "right" and ch == 0)
        if silent:
            assert np.all(x == 0)
        else:
            assert np.max(np.abs(x - ref)) < 2.0 / (1 << (depth - 1))


# ----------------------------------------------------------------------
# Against ltc-tools (independent implementation)

LTCGEN_FPS = {("24", False): "24", ("25", False): "25", ("30", False): "30",
              ("29.97", True): "30000/1001df", ("29.97", False): "30000/1001ndf"}


@needs_ltc_tools
@pytest.mark.parametrize("key,drop", list(LTCGEN_FPS))
def test_bits_match_ltcgen(key, drop, tmp_path):
    """Every bit of ltcgen's words (flags, polarity) equals ours for the same label."""
    path = str(tmp_path / "ref.wav")
    subprocess.run(["ltcgen", "-f", LTCGEN_FPS[(key, drop)], "-t", "00:59:58:10", "-l", "00:00:03:00",
                    "-s", "48000", path], check=True, capture_output=True)
    x, sr = read_wav(path)
    rate = tc.get_rate(key)
    frames = decode(x, sr / float(rate.ltc_fps) / 80)
    assert len(frames) > 60
    for _, bits in frames:
        h, m, s, f = bits_to_label(bits)
        ours = frame_bits(h, m, s, f, drop_frame=drop, family25=rate.is_25_family)[0]
        assert bits == list(ours), f"{h:02d}:{m:02d}:{s:02d}:{f:02d}"


LTCDUMP_LINE = re.compile(r"^([0-9a-f]{8})\s+(\d\d):(\d\d):(\d\d)([:.])(\d\d)\s+\|\s+(-?\d+)\s+(-?\d+)")


def ltcdump(path, fps_arg):
    out = subprocess.run(["ltcdump", "-f", fps_arg, path], check=True, capture_output=True, text=True).stdout
    rows = []
    for line in out.splitlines():
        m = LTCDUMP_LINE.match(line)
        if m:
            ub, h, mi, s, sep, f, a, b = m.groups()
            rows.append((ub, (int(h), int(mi), int(s), int(f)), sep == ".", int(a), int(b)))
    return rows


@needs_ltc_tools
@pytest.mark.parametrize("hms", ["00:59:58", "00:00:58", "23:59:58"])   # tenth minute, dropping minute, midnight
@pytest.mark.parametrize("key,drop", ALL_RATES)
def test_ltcdump_reads_our_files(key, drop, hms, tmp_path):
    sep = ";" if drop else ":"
    p = Plan(JobSpec(rate=key, drop_frame=drop, start=f"{hms}{sep}00", duration=_dur(drop)))
    _ltcdump_check(p, AudioOptions(layout="mono", bit_depth=16), tmp_path)


@needs_ltc_tools
@pytest.mark.parametrize("sr", [44100, 48000, 96000])
@pytest.mark.parametrize("rise", [0.0, 25.0, 50.0, 100.0])
@pytest.mark.parametrize("key,drop", [("25", False), ("29.97", True), ("59.94", True), ("23.976", False)])
def test_ltcdump_sample_rates_and_rise_times(key, drop, sr, rise, tmp_path):
    sep = ";" if drop else ":"
    p = Plan(JobSpec(rate=key, drop_frame=drop, start=f"00:00:58{sep}00", duration=_dur(drop)))
    opts = AudioOptions(sample_rate=sr, rise_us=rise, layout="mono", bit_depth=16)
    _ltcdump_check(p, opts, tmp_path)
    # Precise timing with the interpolating decoder (ltcdump reports whole samples).
    e = LtcEncoder(p, opts)
    y = np.rint(e.render(0, e.total) * 32767) / 32767            # as stored at 16 bit
    errs_us = [(s - e.frame_start(int(round(s / e.frame_len)))) / sr * 1e6 for s, _ in decode(y, e.bit_len)]
    limit = 0.6e6 / sr if rise == 0 else 10.0                     # hard edges sit on whole samples
    assert max(abs(x) for x in errs_us) <= limit, f"{min(errs_us):.1f}..{max(errs_us):.1f} us"


def _ltcdump_check(p, opts, tmp_path):
    drop = p.drop_frame
    e = LtcEncoder(p, opts)
    path = str(tmp_path / "ours.wav")
    write_wav(path, e.to_pcm(e.render(0, e.total)), opts.sample_rate, 1, 16)
    fr = p.rate.ltc_fps
    rows = ltcdump(path, f"{fr.numerator}/{fr.denominator}")
    assert len(rows) == p.ltc_frames - 1          # all but the last (see above)
    offsets = []
    for j, (ub, label, dfflag, a, b) in enumerate(rows):
        assert label == p.ltc_label(j * p.div), f"frame {j}"
        assert dfflag == drop
        assert ub == "00000000"
        if j:   # ltcdump reports whole samples with its own detection lag
            offsets.append(a - e.frame_start(j))
    # Lenient: ltcdump reports whole samples, alternating up to +/-2 samples
    # at fractional frame lengths, and lags slow edges. Bound it by the
    # standard's +/- one video line (63.5 us) and require no trend (drift).
    # Precise timing is checked separately with the interpolating decoder.
    sr = opts.sample_rate
    assert max(abs(o) for o in offsets) / sr * 1e6 <= 63.5
    third = max(1, len(offsets) // 3)
    early, late = offsets[:third], offsets[-third:]
    assert abs(sum(late) / len(late) - sum(early) / len(early)) <= 1.0   # no drift


@needs_ltc_tools
@pytest.mark.slow
def test_one_hour_no_drift(tmp_path):
    """1 h at 29.97 DF: every frame decodes in order, positions don't drift."""
    import time
    import wave
    p = Plan(JobSpec(rate="29.97", drop_frame=True, start="00:59:00;02", duration="01:00:00;00"))
    e = LtcEncoder(p, AudioOptions(layout="mono", bit_depth=16))
    raw = str(tmp_path / "ltc.pcm")
    t0 = time.time()
    info = e.write_pcm(raw)
    gen_s = time.time() - t0
    path = str(tmp_path / "ltc.wav")
    with wave.open(path, "wb") as w, open(raw, "rb") as fh:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(48000)
        while chunk := fh.read(1 << 24):
            w.writeframes(chunk)
    rows = ltcdump(path, "30000/1001")
    assert info["samples"] == e.total
    assert len(rows) == p.ltc_frames - 1
    offsets = [a - e.frame_start(j) for j, (_, label, dfflag, a, _) in enumerate(rows) if j]
    bad = [j for j, row in enumerate(rows) if row[1] != p.ltc_label(j) or not row[2]]
    assert not bad, f"{len(bad)} wrong labels, first at frame {bad[:3]}"
    assert max(abs(o) for o in offsets) <= 2 and max(offsets) - min(offsets) <= 2
    print(f"\n1 h LTC generated in {gen_s:.1f} s; ltcdump offsets {min(offsets):.2f}..{max(offsets):.2f}")
