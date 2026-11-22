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


def test_source_menu_filters_candidates():
    system = {s for s, _ in ff.candidates(env={}, source="system")}
    assert system <= {"PATH", "standard location"} and "standard location" in system
    downloaded_own = {s for s, _ in ff.candidates(env={}, source="downloaded", share=False)}
    assert not downloaded_own & {"installed by vCon", "pip user install", "PATH", "standard location"}
    shared = {s for s, _ in ff.candidates(env={}, source="auto", share=False)}
    assert not shared & {"installed by vCon", "pip user install"}


def test_custom_source_needs_a_path(tmp_path):
    info, tried = ff.find_ffmpeg(source="custom", env={})
    assert info is None and tried[0].reason == "no custom path set"


def test_environment_variable_wins(tmp_path):
    fake = str(tmp_path / "ffmpeg-from-env")
    srcs = ff.candidates(override="/x/ffmpeg", env={ff.ENV_VAR: fake}, source="system")
    assert srcs[0] == (ff.ENV_VAR, fake)


@needs_ffmpeg
def test_probe_movie_reads_counts(tmp_path):
    p = Plan(JobSpec(rate="29.97", drop_frame=True, start="01:00:00;00", duration="00:00:01;00"))
    video = str(tmp_path / "v.mov")
    subprocess.run([FOUND.path, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30000/1001",
                    "-frames:v", str(p.video_frames), "-c:v", "prores_ks", video], check=True)
    e = LtcEncoder(p, AudioOptions())
    pcm = e.write_pcm(str(tmp_path / "ltc.pcm"))
    out = str(tmp_path / "out.mov")
    subprocess.run(ff.mux_command(FOUND.path, video, pcm, out, timecode=p.embedded_timecode), check=True,
                   capture_output=True)
    header = ff.probe_movie(FOUND.path, None, out)
    assert header["width"] == 320 and header["timecode"] == "01:00:00;00" and header["fps_text"] == "29.97"
    if FFPROBE:
        full = ff.probe_movie(FOUND.path, FFPROBE, out)
        assert full["frames"] == p.video_frames and full["samples"] == e.total
        assert full["rate"] == p.rate.fps and full["timecode"] == "01:00:00;00"


@needs_ffmpeg
@pytest.mark.parametrize("vcodec", ["prores_ks", "libx264"])
def test_retime_fixes_60_written_as_5994(vcodec, tmp_path):
    """TD 2025.33230 writes 60 fps as 60000/1001; the mux rescales timestamps."""
    from fractions import Fraction
    p = Plan(JobSpec(rate="60", start="01:00:00:00", duration="00:00:02:00"))
    video = str(tmp_path / "v.mov")
    extra = ["-bf", "2"] if vcodec == "libx264" else []
    subprocess.run([FOUND.path, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=60000/1001",
                    "-frames:v", str(p.video_frames), "-c:v", vcodec, *extra, video], check=True)
    written = ff.video_rate(FOUND.path, FFPROBE, video)
    assert written == Fraction(60000, 1001)
    assert ff.video_rate(FOUND.path, None, video) == Fraction(60000, 1001)      # header-only path
    e = LtcEncoder(p, AudioOptions())
    pcm = e.write_pcm(str(tmp_path / "ltc.pcm"))
    out = str(tmp_path / "out.mov")
    r = subprocess.run(ff.mux_command(FOUND.path, video, pcm, out, timecode=p.embedded_timecode,
                                      retime=written / p.rate.fps), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-500:]
    v_in = _md5([FOUND.path, "-v", "error", "-i", video, "-map", "0:v:0", "-c", "copy", "-f", "md5", "-"])
    v_out = _md5([FOUND.path, "-v", "error", "-i", out, "-map", "0:v:0", "-c", "copy", "-f", "md5", "-"])
    assert v_in == v_out
    if FFPROBE:
        got = ff.probe_movie(FOUND.path, FFPROBE, out)
        assert got["rate"] == Fraction(60) and got["frames"] == p.video_frames and got["samples"] == e.total
        pts = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream=time_base:packet=pts", "-of", "json", out],
                             check=True, capture_output=True, text=True).stdout
        info = json.loads(pts)
        tb = Fraction(info["streams"][0]["time_base"])
        assert sorted(int(x["pts"]) * tb for x in info["packets"]) == [Fraction(i, 60) for i in range(p.video_frames)]
