import numpy as np
import pytest

from ltcfilegen import timecode as tc
from ltcfilegen.job import JobSpec, Plan
from ltcfilegen.ltc import labels_np


@pytest.mark.parametrize("nominal,drop", [(24, 0), (25, 0), (30, 0), (30, 2), (60, 4), (60, 0)])
def test_round_trip_whole_day(nominal, drop):
    per_day = tc.frames_per_day(nominal, drop)
    n = np.arange(0, per_day, 7, dtype=np.int64)
    h, m, s, f = labels_np(n, nominal, drop)
    # every label exists and maps back to the same frame
    back = ((3600 * h + 60 * m + s) * nominal + f) - drop * ((60 * h + m) - (60 * h + m) // 10)
    assert np.array_equal(back, n)
    if drop:
        assert not np.any((s == 0) & (f < drop) & (m % 10 != 0))
    # scalar and vector versions agree
    for k in range(0, len(n), 9973):
        assert tc.frames_to_label(int(n[k]), nominal, drop) == (int(h[k]), int(m[k]), int(s[k]), int(f[k]))


def test_drop_frame_known_values():
    assert tc.frames_to_label(1799, 30, 2) == (0, 0, 59, 29)
    assert tc.frames_to_label(1800, 30, 2) == (0, 1, 0, 2)
    assert tc.frames_to_label(17982, 30, 2) == (0, 10, 0, 0)
    assert tc.frames_per_day(30, 2) == 2589408
    assert tc.label_to_frames(1, 0, 0, 0, 30, 2) == 107892
    assert tc.frames_to_label(tc.frames_per_day(25, 0), 25, 0) == (0, 0, 0, 0)   # 24 h wrap


def test_nonexistent_drop_label_is_rejected_with_suggestion():
    with pytest.raises(tc.TimecodeError, match="did you mean 00:01:00;02"):
        tc.check_label("00:01:00;00", 30, 2)
    assert tc.check_label("00:10:00;00", 30, 2) == (0, 10, 0, 0)
    with pytest.raises(tc.TimecodeError):
        tc.check_label("00:00:00:25", 25, 0)
    with pytest.raises(tc.TimecodeError):
        tc.check_label("24:00:00:00", 25, 0)


def test_drop_frame_only_at_2997_and_5994():
    with pytest.raises(tc.TimecodeError):
        Plan(JobSpec(rate="25", drop_frame=True))
    Plan(JobSpec(rate="29.97", drop_frame=True, start="01:00:00;00"))
    Plan(JobSpec(rate="59.94", drop_frame=True, start="01:00:00;00"))


@pytest.mark.parametrize("dur,frames,end", [
    ("00:01:00;00", 1800, "01:01:00;02"),      # end label skipped -> next real label
    ("00:10:00;00", 17982, "01:10:00;00"),
    ("01:00:00;00", 107892, "02:00:00;00"),
])
def test_drop_frame_durations(dur, frames, end):
    p = Plan(JobSpec(rate="29.97", drop_frame=True, start="01:00:00;00", duration=dur))
    assert p.main_frames == frames
    assert p.end_text == end


def test_preroll_crosses_midnight():
    p = Plan(JobSpec(rate="25", start="00:00:00:00", duration="00:00:05:00", preroll="00:00:10:00"))
    assert p.ltc_text(0) == "23:59:50:00"
    assert p.preroll_frames == 250
    assert p.ltc_text(250) == "00:00:00:00"
    assert p.in_preroll(249) and not p.in_preroll(250)
    assert p.video_frames == 375


def test_preroll_onto_skipped_label_snaps_earlier():
    p = Plan(JobSpec(rate="29.97", drop_frame=True, start="00:01:00;02", duration="00:00:01;00",
                     preroll="00:00:00;02"))
    assert p.ltc_text(0) == "00:00:59;29"


def test_above_30fps_labels_derive_from_ltc():
    p = Plan(JobSpec(rate="59.94", drop_frame=True, start="00:00:59;28", duration="00:00:02;00"))
    assert p.div == 2 and p.N == 30
    texts = [(p.ltc_text(i), p.video_text(i)) for i in range(8)]
    assert texts[:4] == [("00:00:59;28", "00:00:59;56"), ("00:00:59;28", "00:00:59;57"),
                         ("00:00:59;29", "00:00:59;58"), ("00:00:59;29", "00:00:59;59")]
    # minute rollover: LTC skips 00-01, video-frame label skips 00-03
    assert texts[4:6] == [("00:01:00;02", "00:01:00;04"), ("00:01:00;02", "00:01:00;05")]
    assert p.embedded_timecode == "00:00:59;56"


def test_start_with_nonzero_frames_above_30():
    p = Plan(JobSpec(rate="50", start="10:00:00:13", duration="00:00:01:00"))
    assert p.video_text(0) == "10:00:00:26"
    assert p.video_text(1) == "10:00:00:27"
    assert p.video_frames == 50


@pytest.mark.parametrize("key", list(tc.RATES))
def test_sample_ranges_tile_exactly(key):
    p = Plan(JobSpec(rate=key, duration="00:00:10:00"))
    sr = 48000
    total = 0
    for i in range(p.video_frames):
        a, b = p.sample_range(i, sr)
        assert a == total
        total = b
    assert total == p.total_samples(sr)
    assert abs(total - p.video_frames * sr / float(p.rate.fps)) <= 0.5
