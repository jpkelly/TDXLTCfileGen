import hashlib
import json
import os
import subprocess

import pytest

from ltcfilegen import ffmpeg as ff
from ltcfilegen import timecode as tc
from ltcfilegen.job import JobSpec, Plan
from ltcfilegen.ltc import AudioOptions, LtcEncoder

FOUND, _TRIED = ff.find_ffmpeg()
needs_ffmpeg = pytest.mark.skipif(FOUND is None, reason="no usable ffmpeg")
FFPROBE = ff.find_ffprobe(FOUND.path if FOUND else None)

ALL_RATES = [(k, False) for k in tc.RATES] + [("29.97", True), ("59.94", True)]


def test_explicit_setting_that_fails_is_reported_not_replaced(tmp_path):
    missing = str(tmp_path / "nope" / "ffmpeg")
    info, tried = ff.find_ffmpeg(override=missing)
    assert info is None
    assert tried[0].source == "setting" and tried[0].reason == "not found"


def test_non_ffmpeg_binary_is_rejected(tmp_path):
    fake = tmp_path / "ffmpeg"
    fake.write_text("#!/bin/sh\nexit 3\n")
    fake.chmod(0o755)
    info = ff.validate(str(fake))
    assert not info.ok and "code 3" in info.reason


def test_known_locations_are_searched():
    srcs = [s for s, _ in ff.candidates(env={})]
    assert "standard location" in srcs


@needs_ffmpeg
def test_found_ffmpeg_is_validated():
    assert FOUND.ok and FOUND.version


def _md5(cmd):
    return subprocess.run(cmd, check=True, capture_output=True).stdout


@needs_ffmpeg
@pytest.mark.parametrize("key,drop", ALL_RATES)
def test_mux_keeps_video_and_audio_exact(key, drop, tmp_path):
    sep = ";" if drop else ":"
    p = Plan(JobSpec(rate=key, drop_frame=drop, start=f"09:59:59{sep}00", duration=f"00:00:02{sep}00"))
    fr = p.rate.fps
    video = str(tmp_path / "v.mov")
    subprocess.run([FOUND.path, "-v", "error", "-y", "-f", "lavfi",
                    "-i", f"testsrc2=size=320x180:rate={fr.numerator}/{fr.denominator}",
                    "-frames:v", str(p.video_frames), "-c:v", "prores_ks", video], check=True)
    e = LtcEncoder(p, AudioOptions())
    pcm = e.write_pcm(str(tmp_path / "ltc.pcm"))
    out = str(tmp_path / "out.mov")
    r = subprocess.run(ff.mux_command(FOUND.path, video, pcm, out, timecode=p.embedded_timecode),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-800:]

    v_in = _md5([FOUND.path, "-v", "error", "-i", video, "-map", "0:v:0", "-c", "copy", "-f", "md5", "-"])
    v_out = _md5([FOUND.path, "-v", "error", "-i", out, "-map", "0:v:0", "-c", "copy", "-f", "md5", "-"])
    assert v_in == v_out
    a_out = subprocess.run([FOUND.path, "-v", "error", "-i", out, "-map", "0:a:0",
                            "-f", pcm["format"], "-"], check=True, capture_output=True).stdout
    with open(pcm["path"], "rb") as fh:
        assert hashlib.md5(a_out).digest() == hashlib.md5(fh.read()).digest()

    if FFPROBE:
        info = json.loads(subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries",
             "stream=codec_type,nb_frames,r_frame_rate,start_time,duration_ts,time_base:stream_tags=timecode",
             "-of", "json", out], check=True, capture_output=True, text=True).stdout)
        vs = next(s for s in info["streams"] if s["codec_type"] == "video")
        aus = next(s for s in info["streams"] if s["codec_type"] == "audio")
        assert int(vs["nb_frames"]) == p.video_frames
        assert vs["r_frame_rate"] == f"{fr.numerator}/{fr.denominator}"
        assert float(vs["start_time"]) == 0.0 and float(aus["start_time"]) == 0.0
        assert int(aus["duration_ts"]) == e.total
        assert vs["tags"]["timecode"] == p.embedded_timecode
