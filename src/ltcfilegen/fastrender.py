"""Fast render worker: frames from numpy, piped into one ffmpeg that also
adds the LTC track.

The TouchDesigner component starts it as a separate process, so
TouchDesigner stays responsive and the encode is not tied to its frame loop::

    python -m ltcfilegen.fastrender job.json

The job file (JSON) holds:

* ``spec``, ``audio``: :class:`~ltcfilegen.job.JobSpec` and
  :class:`~ltcfilegen.ltc.AudioOptions` fields;
* ``cfg``: the picture settings :class:`~ltcfilegen.picture.Compositor` and
  :func:`~ltcfilegen.picture.frame_state` read;
* ``glyphs``: the ``.npz`` from :func:`~ltcfilegen.picture.save_layers`;
* ``ffmpeg``, ``encoder`` (:class:`~ltcfilegen.ffmpeg.FastEncoder` fields),
  ``timecode`` (or null);
* ``pcm``, ``out``, ``status``, ``log``: file paths.

Progress goes to ``status`` as JSON, replaced whole each time:
``phase`` (audio, video, finishing, done, failed), ``frame``, ``total``,
``fraction``, ``pid``, ``ffmpeg_pid`` and, on failure, ``error``.
The exit code is 0 only when the file is complete.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from fractions import Fraction

from . import ffmpeg as ff
from . import job as jobs
from . import ltc, picture

REPORT_EVERY = 0.25      # seconds between status updates


class _Status:
    def __init__(self, path: str, total: int):
        self.path = path
        self.data = {"phase": "starting", "frame": 0, "total": total, "fraction": 0.0,
                     "pid": os.getpid(), "ffmpeg_pid": None}
        self.last = 0.0

    def put(self, force: bool = False, **kw):
        self.data.update(kw)
        now = time.monotonic()
        if not force and now - self.last < REPORT_EVERY:
            return
        self.last = now
        tmp = self.path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self.data, fh)
        os.replace(tmp, self.path)


def _tail(path: str, n: int = 600) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(max(0, os.path.getsize(path) - n))
            return fh.read().decode("utf-8", "replace").strip()
    except OSError:
        return ""


def run(job: dict) -> int:
    plan = jobs.Plan(jobs.JobSpec(**job["spec"]))
    audio = ltc.AudioOptions(**job["audio"])
    cfg = dict(job["cfg"], slots=plan.rate.nominal)
    n = plan.video_frames
    status = _Status(job["status"], n)

    status.put(force=True, phase="audio")
    enc = ltc.LtcEncoder(plan, audio)
    pcm = enc.write_pcm(job["pcm"], progress=lambda f: status.put(fraction=round(f, 4)))

    layers, info = picture.load_layers(job["glyphs"])
    comp = picture.Compositor(cfg, layers, info)
    encoder = ff.FastEncoder(job["encoder"]["name"], tuple(job["encoder"]["args"]),
                             job["encoder"]["pix_fmt"], bool(job["encoder"]["yuv"]))
    cmd = ff.encode_command(job["ffmpeg"], encoder, cfg["w"], cfg["h"], comp.pix_fmt,
                            Fraction(plan.rate.num, plan.rate.den), pcm, job["out"],
                            timecode=job.get("timecode"))
    kw = {"creationflags": 0x08000000} if os.name == "nt" else {}
    with open(job["log"], "ab") as log:
        log.write((" ".join(cmd) + "\n").encode("utf-8", "replace"))
        log.flush()
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=log, stderr=log, **kw)
    status.put(force=True, phase="video", fraction=0.0, ffmpeg_pid=proc.pid)
    t0 = time.monotonic()
    try:
        for i in range(n):
            texts, flash, slot = picture.frame_state(plan, cfg, i)
            proc.stdin.write(comp.frame(i, texts, flash, slot).data)
            if i % 16 == 15:
                fps = (i + 1) / max(1e-6, time.monotonic() - t0)
                status.put(frame=i + 1, fraction=round((i + 1) / n, 5), fps=round(fps, 1))
        proc.stdin.close()
    except (BrokenPipeError, OSError):
        proc.wait()
        raise RuntimeError(f"ffmpeg stopped (exit {proc.returncode}): {_tail(job['log'])}") from None
    status.put(force=True, phase="finishing", frame=n, fraction=1.0)
    rc = proc.wait()
    if rc != 0:
        raise RuntimeError(f"ffmpeg failed (exit {rc}): {_tail(job['log'])}")
    status.put(force=True, phase="done", seconds=round(time.monotonic() - t0, 2))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: python -m ltcfilegen.fastrender job.json", file=sys.stderr)
        return 2
    with open(argv[0]) as fh:
        job = json.load(fh)
    try:
        return run(job)
    except Exception as exc:
        st = _Status(job["status"], 0)
        try:
            with open(job["status"]) as fh:
                st.data.update(json.load(fh))
        except (OSError, ValueError):
            pass
        st.put(force=True, phase="failed", error=f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
