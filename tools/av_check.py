"""Picture-against-LTC check for lab renders (development tool).

The lab renders a 24-bar binary barcode of the video frame index (top half:
bits LSB-first, bottom half: inverted) with TouchDesigner's stop-frame
recording. For each case in a manifest this script adds the LTC with the
engine (encoder + mux), then checks the FINISHED file:

  1. rate, frame count, audio length, start times, embedded timecode;
  2. every video packet timestamp is exactly i / fps;
  3. every frame's barcode equals its index (no repeats, skips, offsets);
  4. every decodable LTC frame starts on the sample where its video frame
     starts (from the container timestamps) and carries ltc_label(i);
  5. ltcdump (independent decoder) agrees, when installed.

Usage: python tools/av_check.py manifest.json
manifest: {name: {"spec": JobSpec kwargs, "video": path-to-video-only-mov}}
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from fractions import Fraction

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from ltcfilegen import ffmpeg as ff                          # noqa: E402
from ltcfilegen.decode import bits_to_label, decode          # noqa: E402
from ltcfilegen.job import JobSpec, Plan                     # noqa: E402
from ltcfilegen.ltc import AudioOptions, LtcEncoder          # noqa: E402

BARS = 24


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, **kw)


def probe(ffprobe, path, extra):
    return json.loads(run([ffprobe, "-v", "error", *extra, "-of", "json", path], text=True).stdout)


def barcodes(ffmpeg, path, w, h):
    rows = []
    for y in (h // 4, (3 * h) // 4):
        raw = run([ffmpeg, "-v", "error", "-i", path, "-map", "0:v:0",
                   "-vf", f"format=gray,crop={w}:1:0:{y}", "-f", "rawvideo", "-"]).stdout
        rows.append([raw[i:i + w] for i in range(0, len(raw), w)])
    out, bad = [], 0
    for top, bot in zip(*rows):
        v = 0
        for b in range(BARS):
            x = int((b + 0.5) * w / BARS)
            if (top[x] > 128) == (bot[x] > 128):
                bad += 1
            v |= int(top[x] > 128) << b
        out.append(v)
    return out, bad


def check_case(name, case, workdir, ffmpeg, ffprobe):
    problems = []
    plan = Plan(JobSpec(**case["spec"]))
    opts = AudioOptions()
    enc = LtcEncoder(plan, opts)
    pcm = enc.write_pcm(os.path.join(workdir, f"{name}_ltc.pcm"))
    out = os.path.join(workdir, f"{name}.mov")
    r = subprocess.run(ff.mux_command(ffmpeg, case["video"], pcm, out, timecode=plan.embedded_timecode),
                       capture_output=True, text=True)
    if r.returncode:
        return [f"mux failed: {r.stderr[-300:]}"], {}
    fps = plan.rate.fps
    sr = opts.sample_rate

    # 1. streams
    info = probe(ffprobe, out, ["-show_entries",
                                "stream=codec_type,codec_tag_string,width,height,nb_frames,r_frame_rate,"
                                "time_base,start_time,duration_ts:stream_tags=timecode"])
    vs = next(s for s in info["streams"] if s["codec_type"] == "video")
    au = next(s for s in info["streams"] if s["codec_type"] == "audio")
    if vs["r_frame_rate"] != f"{fps.numerator}/{fps.denominator}":
        problems.append(f"rate {vs['r_frame_rate']} != {fps}")
    if int(vs["nb_frames"]) != plan.video_frames:
        problems.append(f"{vs['nb_frames']} video frames, expected {plan.video_frames}")
    if int(au["duration_ts"]) != enc.total:
        problems.append(f"{au['duration_ts']} audio samples, expected {enc.total}")
    if float(vs["start_time"]) != 0.0 or float(au["start_time"]) != 0.0:
        problems.append(f"start times {vs['start_time']} / {au['start_time']}")
    if vs.get("tags", {}).get("timecode") != plan.embedded_timecode:
        problems.append(f"timecode track {vs.get('tags', {}).get('timecode')} != {plan.embedded_timecode}")

    # 2. packet timestamps -> exact start sample of every video frame
    pk = probe(ffprobe, out, ["-select_streams", "v:0", "-show_entries", "packet=pts"])
    tb = Fraction(vs["time_base"])
    pts = sorted(int(p["pts"]) for p in pk["packets"])
    bad_pts = [i for i, p in enumerate(pts) if p * tb != Fraction(i) / fps]
    if bad_pts:
        problems.append(f"{len(bad_pts)} video timestamps off, first at frame {bad_pts[0]}")
    vstart = [p * tb * sr for p in pts]                              # exact, in samples

    # 3. barcodes
    codes, badbits = barcodes(ffmpeg, out, int(vs["width"]), int(vs["height"]))
    if codes != list(range(plan.video_frames)):
        first = next((i for i, c in enumerate(codes) if c != i), len(codes))
        problems.append(f"barcode mismatch at frame {first} (got {codes[first:first + 3]})")
    if badbits:
        problems.append(f"{badbits} unreadable barcode bits")

    # 4. LTC from the finished file vs picture timestamps
    raw = run([ffmpeg, "-v", "error", "-i", out, "-map", "0:a:0", "-f", "s32le", "-acodec", "pcm_s32le", "-"]).stdout
    a = np.frombuffer(raw, dtype="<i4").reshape(-1, opts.channels)[:, 0].astype(np.float64) / 2147483647.0
    frames = decode(a, enc.bit_len)
    seen, worst = set(), 0.0
    for start, bits in frames:
        j = int(round(start / enc.frame_len))
        i = j * plan.div
        seen.add(j)
        dev = abs(start - float(vstart[i]))
        worst = max(worst, dev)
        if dev > 0.6:
            problems.append(f"LTC frame {j} starts {start:.2f}, video frame {i} at {float(vstart[i]):.2f}")
        if bits_to_label(bits) != plan.ltc_label(i):
            problems.append(f"LTC frame {j} reads {bits_to_label(bits)}, expected {plan.ltc_label(i)}")
        if bits[10] != int(plan.drop_frame):
            problems.append(f"LTC frame {j} drop-frame flag {bits[10]}")
    missing = set(range(1, plan.ltc_frames - 1)) - seen
    if missing:
        problems.append(f"{len(missing)} LTC frames not decoded, e.g. {sorted(missing)[:3]}")

    # 5. ltcdump
    dumped = None
    if shutil.which("ltcdump"):
        import wave
        wav = os.path.join(workdir, f"{name}_ch0.wav")
        with wave.open(wav, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
            w.writeframes(np.clip(np.rint(a * 32767), -32767, 32767).astype("<i2").tobytes())
        lf = plan.rate.ltc_fps
        txt = run(["ltcdump", "-f", f"{lf.numerator}/{lf.denominator}", wav], text=True).stdout
        labels = [tuple(int(x) for x in m.groups()) for m in
                  re.finditer(r"^[0-9a-f]{8}\s+(\d\d):(\d\d):(\d\d)[:.](\d\d)", txt, re.M)]
        want = [plan.ltc_label(j * plan.div) for j in range(plan.ltc_frames - 1)]
        dumped = len(labels)
        if labels != want:
            problems.append(f"ltcdump: {len(labels)} labels, mismatch vs plan")

    stats = {"frames": plan.video_frames, "ltc_decoded": len(frames), "ltc_frames": plan.ltc_frames,
             "worst_start_dev_samples": round(worst, 3), "ltcdump_frames": dumped,
             "codec": vs["codec_tag_string"], "first": plan.ltc_text(0), "tc_track": plan.embedded_timecode}
    return problems, stats


def main():
    manifest_path = sys.argv[1]
    workdir = os.path.dirname(os.path.abspath(manifest_path))
    manifest = json.load(open(manifest_path))
    info, _ = ff.find_ffmpeg()
    if not info:
        sys.exit("no usable ffmpeg found")
    ffprobe = ff.find_ffprobe(info.path)
    if not ffprobe:
        sys.exit("this check needs ffprobe")
    failed = 0
    for name, case in manifest.items():
        problems, stats = check_case(name, case, workdir, info.path, ffprobe)
        status = "PASS" if not problems else "FAIL"
        failed += bool(problems)
        print(f"{status} {name:20s} {stats}", flush=True)
        for p in problems[:8]:
            print(f"     - {p}")
    print(f"\n{len(manifest) - failed}/{len(manifest)} cases passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
