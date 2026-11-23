"""Write the LTC track as a WAV file in a separate process.

The component runs it so TouchDesigner stays responsive (Python threads
inside TouchDesigner get little time: an hour took about 100 s there, 10 s
here)::

    python -m ltcfilegen.wavwrite job.json

The job file holds ``spec`` and ``audio`` (as for fastrender), ``out`` (the
WAV to write), ``status`` (progress JSON, as fastrender writes it) and
optionally ``pcm``: a raw PCM file to wrap instead of rendering the LTC.
"""

from __future__ import annotations

import json
import sys

from . import job as jobs
from . import ltc
from .fastrender import _Status


def run(job: dict) -> int:
    plan = jobs.Plan(jobs.JobSpec(**job["spec"]))
    audio = ltc.AudioOptions(**job["audio"])
    status = _Status(job["status"], 0)
    status.put(force=True, phase="wav")
    progress = lambda f: status.put(fraction=round(f, 4))
    if job.get("pcm"):
        ltc.pcm_to_wav(job["pcm"], job["out"], audio, progress=progress)
    else:
        ltc.LtcEncoder(plan, audio).write_wav(job["out"], progress=progress)
    status.put(force=True, phase="done", fraction=1.0)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: python -m ltcfilegen.wavwrite job.json", file=sys.stderr)
        return 2
    with open(argv[0]) as fh:
        job = json.load(fh)
    try:
        return run(job)
    except Exception as exc:
        _Status(job["status"], 0).put(force=True, phase="failed", error=f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
