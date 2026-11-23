"""ffmpeg fast engine: the encode command and the worker, end to end.

Glyphs come from the fake font in test_picture; the comparison with
TouchDesigner's picture runs inside the component (LtcGenExt.FastGlyphs).
"""

import dataclasses
import json
import os
import subprocess
import sys
from fractions import Fraction

import numpy as np
import pytest

from ltcfilegen import ffmpeg as ff
from ltcfilegen import fastrender, picture as pic
from ltcfilegen.job import JobSpec, Plan
from ltcfilegen.ltc import AudioOptions, LtcEncoder

from test_picture import H, W, _setup

FOUND, _ = ff.find_ffmpeg()
needs_ffmpeg = pytest.mark.skipif(FOUND is None, reason="no usable ffmpeg")
FFPROBE = ff.find_ffprobe(FOUND.path if FOUND else None)

PCM = {"path": "ltc.pcm", "sample_rate": 48000, "channels": 2, "format": "s24le"}


def test_encode_command_reads_frames_from_stdin_and_tags_bt709():
    enc = ff.FAST_ENCODERS["prores422hq"][1]
    cmd = ff.encode_command("ffmpeg", enc, 1920, 1080, True, Fraction(30000, 1001), PCM, "out.mov",
                            timecode="01:00:00;00")
    s = " ".join(cmd)
    assert "-f rawvideo -pix_fmt gray -color_range pc -s 1920x1080 -framerate 30000/1001 -i pipe:0" in s
    assert "-f s24le -ar 48000 -ac 2 -i ltc.pcm" in s
    vf = cmd[cmd.index("-vf") + 1]
    assert "out_color_matrix=bt709" in vf and "out_range=tv" in vf and "accurate_rnd" in vf
    assert "color_trc=iec61966-2-1" in vf and "range=tv" in vf
    assert cmd[cmd.index("-c:v") + 1] == "prores_ks" and cmd[cmd.index("-pix_fmt", cmd.index("-c:v")) + 1] == "yuv422p10le"
    assert cmd[cmd.index("-c:a") + 1] == "pcm_s24le"
    assert cmd[cmd.index("-timecode") + 1] == "01:00:00;00"
    assert cmd[-1] == "out.mov"


def test_hap_keeps_rgb_and_is_not_range_converted():
    enc = ff.FAST_ENCODERS["hap"][0]
    cmd = ff.encode_command("ffmpeg", enc, 64, 64, False, Fraction(25), PCM, "o.mov")
    vf = cmd[cmd.index("-vf") + 1]
    assert vf.startswith("format=rgba,") and "out_range" not in vf
    assert "-pix_fmt rgb24" in " ".join(cmd) and "-chunks 12" in " ".join(cmd)


def test_every_fast_codec_has_an_encoder_listed():
    assert set(ff.FAST_ENCODERS) == {"hap", "hapq", "prores422hq", "prores422", "h264", "h265"}
    for encs in ff.FAST_ENCODERS.values():
        assert encs and all(isinstance(e, ff.FastEncoder) for e in encs)


def _job(tmp_path, codec, rate="59.94", df=True, colour=False):
    plan, cfg, layers, info, _ = _setup(rate=rate, df=df, start="00:59:58;00" if df else "00:59:58:00",
                                        colour=colour)
    glyphs = str(tmp_path / "g.npz")
    pic.save_layers(glyphs, layers, info)
    enc = ff.fast_encoder(FOUND.path, codec)
    if enc is None:
        pytest.skip(f"this ffmpeg cannot encode {codec}")
    keys = ("w", "h", "fg", "bg", "main", "secondary", "countdown", "flash", "marker", "marker_h",
            "barcode", "strip_h")
    job = {"spec": dataclasses.asdict(plan.spec), "audio": dataclasses.asdict(AudioOptions()),
           "cfg": {k: cfg[k] for k in keys}, "glyphs": glyphs, "pcm": str(tmp_path / "a.pcm"),
           "out": str(tmp_path / "out.mov"), "status": str(tmp_path / "st.json"),
           "log": str(tmp_path / "ff.log"), "ffmpeg": FOUND.path,
           "encoder": dataclasses.asdict(enc), "timecode": plan.embedded_timecode}
    path = tmp_path / "job.json"
    path.write_text(json.dumps(job))
    return plan, cfg, layers, info, job, str(path)


def _decode(path, w=W, h=H):
    raw = subprocess.run([FOUND.path, "-v", "error", "-i", path, "-map", "0:v:0", "-f", "rawvideo",
                          "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, h, w, 3)


@needs_ffmpeg
@pytest.mark.parametrize("codec", ["hap", "prores422hq", "h264"])
def test_worker_makes_a_complete_verified_movie(tmp_path, codec):
    plan, cfg, layers, info, job, path = _job(tmp_path, codec)
    env = dict(os.environ, PYTHONPATH=os.path.join(os.path.dirname(__file__), "..", "src"))
    rc = subprocess.run([sys.executable, "-m", "ltcfilegen.fastrender", path], env=env).returncode
    st = json.loads(open(job["status"]).read())
    assert rc == 0 and st["phase"] == "done", st
    got = ff.probe_movie(FOUND.path, FFPROBE, job["out"])
    assert got.get("frames", plan.video_frames) == plan.video_frames
    assert (got["width"], got["height"]) == (W, H)
    if "samples" in got:
        assert got["samples"] == LtcEncoder(plan, AudioOptions()).total
    if "timecode" in got:
        assert got["timecode"] == plan.embedded_timecode
    frames = _decode(job["out"])
    assert len(frames) == plan.video_frames
    # The debug barcode survives the codec: read frame numbers back.
    for i in (0, 1, plan.video_frames // 2, plan.video_frames - 1):
        bars = frames[i, 4, (np.arange(24) * W // 24 + W // 48)].mean(axis=1) > 128
        assert sum(int(b) << k for k, b in enumerate(bars)) == i


@needs_ffmpeg
def test_worker_reports_a_failing_encoder(tmp_path):
    plan, cfg, layers, info, job, path = _job(tmp_path, "h264")
    job["encoder"]["args"] = ["-no-such-option", "1"]
    open(path, "w").write(json.dumps(job))
    assert fastrender.main([path]) == 1
    st = json.loads(open(job["status"]).read())
    assert st["phase"] == "failed" and "ffmpeg" in st["error"]
