"""Find, validate, install and run ffmpeg.

TouchDesigner's movie writer records frame-exact video only, so the LTC
audio is added afterwards with ffmpeg (stream copy, no re-encode). The fast
engine instead pipes frames made in Python straight into an ffmpeg encoder
(:func:`encode_command`).

Discovery follows the approach of vCon (same author), with these additions:

* an explicit path (setting or ``LTCFILEGEN_FFMPEG``) is tried first;
* well-known install folders are checked directly, because TouchDesigner
  opened from the Finder only sees ``/usr/bin:/bin:/usr/sbin:/sbin`` and
  never finds a Homebrew or MacPorts ffmpeg on PATH;
* vCon's own downloaded copy is reused, so users of both tools download once;
* a candidate only counts once it has *run* and reported the features this
  tool needs (mov muxer, PCM encoders); the result is cached per file.

ffprobe is optional: the pip-installed ffmpeg doesn't include it, so
nothing here requires it.
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from fractions import Fraction

APP_DIR_NAME = "LTCfileGen"
ENV_VAR = "LTCFILEGEN_FFMPEG"
REQUIRED_MUXERS = ("mov",)
REQUIRED_ENCODERS = ("pcm_s16le", "pcm_s24le")


def user_data_dir(app: str = APP_DIR_NAME) -> str:
    """Per-user data folder (never inside the repo or the TouchDesigner app)."""
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Application Support", app)
    if os.name == "nt":
        return os.path.join(os.environ.get("LOCALAPPDATA") or home, app)
    return os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share"), app)


def pylibs_dir() -> str:
    """Where Install Now puts the imageio-ffmpeg package."""
    return os.path.join(user_data_dir(), "pylibs")


def _exe(name: str) -> str:
    return name + ".exe" if os.name == "nt" else name


def known_locations() -> list[str]:
    """Usual ffmpeg install paths that may not be on TouchDesigner's PATH."""
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/opt/local/bin/ffmpeg"]
    if os.name == "nt":
        la = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        pd = os.environ.get("ProgramData") or r"C:\ProgramData"
        return [os.path.join(la, "Microsoft", "WinGet", "Links", "ffmpeg.exe"),
                os.path.join(pd, "chocolatey", "bin", "ffmpeg.exe"),
                os.path.join(home, "scoop", "shims", "ffmpeg.exe"),
                r"C:\ffmpeg\bin\ffmpeg.exe"]
    return ["/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/snap/bin/ffmpeg"]


def _imageio_binaries(folder: str) -> list[str]:
    hits = glob.glob(os.path.join(folder, "imageio_ffmpeg", "binaries", "ffmpeg-*"))
    return sorted((h for h in hits if not h.endswith((".py", ".md", ".txt"))), reverse=True)


#: Source menu: which kinds of ffmpeg discovery may use.
SOURCES = ("auto", "downloaded", "system", "custom")

#: Candidate source labels -> source-menu category.
CATEGORY = {"setting": "custom", ENV_VAR: "custom",
            "imageio-ffmpeg": "downloaded", "installed by this tool": "downloaded",
            "installed by vCon": "shared", "pip user install": "shared",
            "PATH": "system", "standard location": "system"}


def candidates(override: str | None = None, env: dict | None = None,
               source: str = "auto", share: bool = True) -> list[tuple[str, str]]:
    """(source, path) pairs in the order they are tried. Paths may not exist.

    ``source`` limits discovery to one category of the source menu:
    ``downloaded`` (imageio-ffmpeg copies), ``system`` (PATH and standard
    install folders such as Homebrew), ``custom`` (only ``override``) or
    ``auto`` (all, in order). ``share=False`` ignores copies downloaded by
    other tools (vCon, pip/pipx installs). The environment variable always
    wins, for managed machines.
    """
    env = os.environ if env is None else env
    home = os.path.expanduser("~")
    out: list[tuple[str, str]] = []
    if env.get(ENV_VAR):
        out.append((ENV_VAR, env[ENV_VAR]))
    if override and source in ("auto", "custom"):
        out.append(("setting", override))
    try:
        import imageio_ffmpeg  # type: ignore
        out.append(("imageio-ffmpeg", imageio_ffmpeg.get_ffmpeg_exe()))
    except Exception:
        pass
    for p in _imageio_binaries(pylibs_dir()):
        out.append(("installed by this tool", p))
    for p in _imageio_binaries(os.path.join(user_data_dir("vCon"), "pylibs")):
        out.append(("installed by vCon", p))
    for pat in (f"{home}/.local/pipx/venvs/*/lib/python*/site-packages",
                f"{home}/Library/Python/*/lib/python/site-packages",
                f"{home}/.local/lib/python*/site-packages"):
        for site in glob.glob(pat):
            for p in _imageio_binaries(site):
                out.append(("pip user install", p))
    which = shutil.which(_exe("ffmpeg"))
    if which:
        out.append(("PATH", which))
    for p in known_locations():
        out.append(("standard location", p))

    def allowed(src: str) -> bool:
        cat = CATEGORY[src]
        if src == ENV_VAR:
            return True
        if cat == "shared":
            return share and source in ("auto", "downloaded")
        return source == "auto" or cat == source
    out = [(s, p) for s, p in out if allowed(s)]
    seen, unique = set(), []
    for src, p in out:
        key = os.path.normcase(os.path.abspath(p))
        if key not in seen:
            seen.add(key)
            unique.append((src, p))
    return unique


@dataclass
class FfmpegInfo:
    path: str
    source: str = ""
    ok: bool = False
    version: str = ""
    reason: str = ""


_cache: dict[tuple, FfmpegInfo] = {}


def _run(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = 0x08000000          # CREATE_NO_WINDOW: no console flash
    return subprocess.run(args, capture_output=True, timeout=timeout,
                          stdin=subprocess.DEVNULL, **kw)


def validate(path: str, source: str = "", timeout: float = 10.0) -> FfmpegInfo:
    """Run the binary and check it has what the mux needs. Cached per file."""
    info = FfmpegInfo(path=path, source=source)
    try:
        st = os.stat(path)
    except OSError:
        info.reason = "not found"
        return info
    key = (os.path.abspath(path), st.st_mtime, st.st_size)
    if key in _cache:
        cached = _cache[key]
        return FfmpegInfo(path, source, cached.ok, cached.version, cached.reason)
    try:
        v = _run([path, "-hide_banner", "-version"], timeout)
        if v.returncode != 0:
            info.reason = f"exits with code {v.returncode}"
        else:
            first = v.stdout.decode("utf-8", "replace").splitlines()[:1]
            parts = first[0].split() if first else []
            info.version = parts[2] if len(parts) > 2 and parts[0] == "ffmpeg" else (first[0][:40] if first else "?")
            mux = _run([path, "-hide_banner", "-muxers"], timeout).stdout.decode("utf-8", "replace")
            enc = _run([path, "-hide_banner", "-encoders"], timeout).stdout.decode("utf-8", "replace")
            mux_names = {tok for line in mux.splitlines() for tok in line.split()[1:2]}
            enc_names = {tok for line in enc.splitlines() for tok in line.split()[1:2]}
            missing = [m for m in REQUIRED_MUXERS if m not in mux_names] + \
                      [e for e in REQUIRED_ENCODERS if e not in enc_names]
            if missing:
                info.reason = "missing " + ", ".join(missing)
            else:
                info.ok = True
    except subprocess.TimeoutExpired:
        info.reason = "did not respond"
    except OSError as exc:
        info.reason = f"cannot run ({exc.strerror or exc})"
    _cache[key] = FfmpegInfo(path, source, info.ok, info.version, info.reason)
    return info


def find_ffmpeg(override: str | None = None, env: dict | None = None,
                source: str = "auto", share: bool = True) -> tuple[FfmpegInfo | None, list[FfmpegInfo]]:
    """First usable ffmpeg, plus every candidate tried (for the setup dialog).

    An explicit setting that fails is reported, not silently replaced.
    """
    if source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}")
    tried: list[FfmpegInfo] = []
    if source == "custom" and not override and not (env or os.environ).get(ENV_VAR):
        return None, [FfmpegInfo("", "setting", False, "", "no custom path set")]
    for src, path in candidates(override, env, source, share):
        if not os.path.isfile(path):
            if src in ("setting", ENV_VAR):
                tried.append(FfmpegInfo(path, src, False, "", "not found"))
                return None, tried
            continue
        info = validate(path, src)
        tried.append(info)
        if info.ok:
            return info, tried
        if src in ("setting", ENV_VAR):
            return None, tried
    return None, tried


def find_ffprobe(ffmpeg_path: str | None) -> str | None:
    """ffprobe next to the chosen ffmpeg, else on PATH or a standard location.

    Set LTCFILEGEN_NO_FFPROBE=1 to test the ffmpeg-only path most users have
    (the pip-installed ffmpeg ships without ffprobe).
    """
    if os.environ.get("LTCFILEGEN_NO_FFPROBE"):
        return None
    names = [_exe("ffprobe")]
    if ffmpeg_path:
        d = os.path.dirname(ffmpeg_path)
        for n in names:
            p = os.path.join(d, n)
            if os.path.isfile(p):
                return p
    which = shutil.which(_exe("ffprobe"))
    if which:
        return which
    for p in known_locations():
        q = os.path.join(os.path.dirname(p), _exe("ffprobe"))
        if os.path.isfile(q):
            return q
    return None


# ----------------------------------------------------------------------
# Install (imageio-ffmpeg into this tool's user folder, no admin rights)


def _python_candidates() -> list[str]:
    """Python interpreters to try, TouchDesigner's own first.

    Inside TouchDesigner ``sys.executable`` is TouchDesigner itself, so the
    interpreter is located explicitly.
    """
    cands = []
    for prefix in (sys.prefix, getattr(sys, "base_prefix", "")):
        if prefix:
            if os.name == "nt":
                cands.append(os.path.join(prefix, "python.exe"))
            else:
                ver = f"python{sys.version_info.major}.{sys.version_info.minor}"
                cands += [os.path.join(prefix, "bin", ver), os.path.join(prefix, "bin", "python3")]
    for name in (("py", "python", "python3") if os.name == "nt" else ("python3", "python")):
        found = shutil.which(name)
        if found:
            cands.append(found)
    seen, out = set(), []
    for c in cands:
        if c and os.path.isfile(c) and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def python_for_pip() -> str | None:
    """A Python that can run pip, preferring TouchDesigner's own."""
    for cand in _python_candidates():
        try:
            if _run([cand, "-m", "pip", "--version"], 30).returncode == 0:
                return cand
        except Exception:
            continue
    return None


def install_command(python: str, dest: str | None = None) -> list[str]:
    return [python, "-m", "pip", "install", "--upgrade", "--target", dest or pylibs_dir(), "imageio-ffmpeg"]


def start_install(python: str, log_path: str | None = None) -> subprocess.Popen:
    """Start the download in the background; poll the returned process."""
    dest = pylibs_dir()
    os.makedirs(dest, exist_ok=True)
    log = open(log_path or os.path.join(dest, "install.log"), "wb")
    kw = {"creationflags": 0x08000000} if os.name == "nt" else {}
    return subprocess.Popen(install_command(python, dest), stdout=log, stderr=log,
                            stdin=subprocess.DEVNULL, **kw)


# ----------------------------------------------------------------------
# Read back a finished file


def probe_movie(ffmpeg: str, ffprobe: str | None, path: str, timeout: float = 60.0) -> dict:
    """Rate, frame count, audio samples and timecode of a finished movie.

    Uses ffprobe when available (exact counts from the container). With
    ffmpeg only, reads the stream header: rate and duration, no counts.
    Keys missing from the result could not be read.
    """
    import json
    from fractions import Fraction
    out: dict = {}
    if ffprobe:
        r = _run([ffprobe, "-v", "error", "-show_entries",
                  "stream=codec_type,codec_tag_string,width,height,nb_frames,r_frame_rate,"
                  "duration_ts,sample_rate,channels:stream_tags=timecode",
                  "-of", "json", path], timeout)
        info = json.loads(r.stdout.decode("utf-8", "replace") or "{}")
        for s in info.get("streams", []):
            if s.get("codec_type") == "video" and "rate" not in out:
                out.update(rate=Fraction(s["r_frame_rate"]), frames=int(s.get("nb_frames", 0) or 0),
                           width=int(s["width"]), height=int(s["height"]), codec=s.get("codec_tag_string", ""))
                if s.get("tags", {}).get("timecode"):
                    out["timecode"] = s["tags"]["timecode"]
            elif s.get("codec_type") == "audio" and "samples" not in out:
                out.update(samples=int(s.get("duration_ts", 0) or 0), sample_rate=int(s["sample_rate"]),
                           channels=int(s["channels"]))
        return out
    import re
    r = _run([ffmpeg, "-hide_banner", "-i", path], timeout)
    text = r.stderr.decode("utf-8", "replace")
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", text)
    if m:
        out["duration"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    m = re.search(r"Video: .*?, (\d+)x(\d+).*?, ([\d.]+) (?:fps|tbr)", text)
    if m:
        out.update(width=int(m.group(1)), height=int(m.group(2)), fps_text=m.group(3))
    m = re.search(r"timecode\s*:\s*(\S+)", text)
    if m:
        out["timecode"] = m.group(1)
    return out


def alpha_range(ffmpeg: str, path: str, frame: int, w: int, h: int,
                timeout: float = 60.0) -> tuple[int, int] | None:
    """(lowest, highest) 8-bit alpha in video frame ``frame`` of a finished
    movie, or None if it could not be decoded. A stream without alpha
    decodes as fully opaque: (255, 255)."""
    r = _run([ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-i", path, "-map", "0:v:0",
              "-vf", f"select=eq(n\\,{int(frame)})", "-frames:v", "1",
              "-f", "rawvideo", "-pix_fmt", "rgba", "-"], timeout)
    if r.returncode or len(r.stdout) != w * h * 4:
        return None
    import numpy as np
    a = np.frombuffer(r.stdout, np.uint8)[3::4]
    return int(a.min()), int(a.max())


# ----------------------------------------------------------------------
# Mux


#: Common rates, to identify the exact rate from ffmpeg's rounded header text.
KNOWN_RATES = tuple(Fraction(n, d) for n, d in (
    (24000, 1001), (24, 1), (25, 1), (30000, 1001), (30, 1), (48000, 1001), (48, 1),
    (50, 1), (60000, 1001), (60, 1), (100, 1), (120000, 1001), (120, 1)))


def video_rate(ffmpeg: str, ffprobe: str | None, path: str, timeout: float = 30.0) -> Fraction | None:
    """Exact frame rate written in a movie's video stream (None if unreadable).

    ffprobe gives the exact fraction. With ffmpeg only, the header's rounded
    "59.94 fps" text is matched against KNOWN_RATES.
    """
    if ffprobe:
        r = _run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                  "stream=r_frame_rate", "-of", "csv=p=0", path], timeout)
        text = r.stdout.decode("utf-8", "replace").strip().splitlines()
        try:
            return Fraction(text[0]) if text else None
        except (ValueError, ZeroDivisionError):
            return None
    import re
    r = _run([ffmpeg, "-hide_banner", "-i", path], timeout)
    m = re.search(r"Video: .*?, ([\d.]+) fps", r.stderr.decode("utf-8", "replace"))
    if not m:
        return None
    shown = float(m.group(1))
    hits = [k for k in KNOWN_RATES if abs(float(k) - shown) < 0.006]
    return hits[0] if len(hits) == 1 else None


def mux_command(ffmpeg: str, video: str, pcm: dict, out: str,
                timecode: str | None = None, progress_path: str | None = None,
                retime: Fraction | None = None, bt709: bool = False) -> list[str]:
    """Copy the video untouched and add the raw PCM LTC track.

    ``pcm`` is the dict returned by ``LtcEncoder.write_pcm``.

    ``bt709`` relabels a ProRes stream's colour matrix as BT.709.

    ``retime`` rescales the video timestamps (still a stream copy) by
    ``written_rate / wanted_rate``. TouchDesigner's Movie File Out writes a
    whole-number rate (e.g. 60) at the project's rate when the project runs
    at a 1001 rate (e.g. 59.94); the frames are right, only the timestamps
    are labelled wrong, so ``retime=Fraction(1000, 1001)`` fixes them. The
    component avoids this by rendering in a 60 fps project; this is the
    safety net. pts, dts and durations are scaled separately so H.264/H.265
    frame reordering survives.
    """
    codec = {"s16le": "pcm_s16le", "s24le": "pcm_s24le"}[pcm["format"]]
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y",
           "-i", video,
           "-f", pcm["format"], "-ar", str(pcm["sample_rate"]), "-ac", str(pcm["channels"]),
           "-i", pcm["path"],
           "-map", "0:v:0", "-map", "1:a:0",
           "-c:v", "copy", "-c:a", codec,
           "-metadata:s:a:0", "title=LTC", "-metadata:s:a:0", "handler_name=LTC"]
    bsf = []
    if retime is not None and retime != 1:
        a, b = retime.numerator, retime.denominator
        bsf.append(f"setts=pts=PTS*{a}/{b}:dts=DTS*{a}/{b}:duration=DURATION*{a}/{b}")
    if bt709:
        # ProRes 4444 from TouchDesigner: BT.709 pixels labelled BT.601, in
        # every frame header and in the container. Relabel both (no re-encode).
        bsf.append("prores_metadata=colorspace=bt709")
        cmd += ["-colorspace", "bt709"]
    if bsf:
        cmd += ["-bsf:v", ",".join(bsf)]
    if timecode:
        cmd += ["-timecode", timecode]
    if progress_path:
        cmd += ["-progress", progress_path]
    cmd.append(out)
    return cmd


# ----------------------------------------------------------------------
# Fast engine: ffmpeg encodes frames generated in Python (ltcfilegen.picture)
# and adds the LTC in the same pass. NotchLC has no ffmpeg encoder.

#: Colour tags TouchDesigner's Movie File Out writes (sRGB output, BT.709).
#: Set on the frames (setparams), since output options lose to frame tags.
COLOUR_TAGS = "setparams=color_primaries=bt709:color_trc=iec61966-2-1:colorspace=bt709"


@dataclass(frozen=True)
class FastEncoder:
    """An ffmpeg encoder setup matching one of the component's codecs."""

    name: str                 # ffmpeg encoder
    args: tuple               # encoder options
    pix_fmt: str              # pixel format handed to the encoder
    yuv: bool                 # converted to limited-range BT.709 YUV


_H26X_RATE = ("-maxrate", "10M", "-bufsize", "20M", "-g", "15")   # TD: 10 Mb/s peak, keyframe every 15

#: Candidates per codec, best first. Settings follow what Movie File Out
#: writes (HAP in 12 chunks, ProRes 4:2:2 10-bit, H.264/H.265 4:2:0 with a
#: keyframe every 15 frames, hvc1 tag). Quality-based rate control with a
#: peak cap keeps mostly-black timecode files small.
FAST_ENCODERS: dict[str, tuple[FastEncoder, ...]] = {
    "hap": (FastEncoder("hap", ("-format", "hap", "-chunks", "12", "-compressor", "snappy"), "rgba", False),),
    "hapalpha": (FastEncoder("hap", ("-format", "hap_alpha", "-chunks", "12", "-compressor", "snappy"),
                             "rgba", False),),
    "hapq": (FastEncoder("hap", ("-format", "hap_q", "-chunks", "12", "-compressor", "snappy"), "rgba", False),),
    "prores422hq": (FastEncoder("prores_videotoolbox", ("-profile:v", "hq"), "p210le", True),
                    FastEncoder("prores_ks", ("-profile:v", "3", "-vendor", "apl0"), "yuv422p10le", True)),
    "prores422": (FastEncoder("prores_videotoolbox", ("-profile:v", "standard"), "p210le", True),
                  FastEncoder("prores_ks", ("-profile:v", "2", "-vendor", "apl0"), "yuv422p10le", True)),
    "prores4444": (FastEncoder("prores_videotoolbox", ("-profile:v", "4444"), "p410le", True),
                   FastEncoder("prores_ks", ("-profile:v", "4", "-vendor", "apl0"), "yuv444p10le", True)),
    "prores4444alpha": (FastEncoder("prores_videotoolbox", ("-profile:v", "4444"), "ayuv64le", True),
                        FastEncoder("prores_ks", ("-profile:v", "4", "-vendor", "apl0"), "yuva444p10le", True)),
    "h264": (FastEncoder("libx264", ("-preset", "fast", "-profile:v", "high", "-crf", "18") + _H26X_RATE,
                         "yuv420p", True),
             FastEncoder("h264_videotoolbox", ("-profile:v", "high", "-b:v", "8M") + _H26X_RATE,
                         "yuv420p", True)),
    # VideoToolbox first: about twice as fast as x265 here (M4 Max).
    "h265": (FastEncoder("hevc_videotoolbox", ("-b:v", "8M", "-tag:v", "hvc1") + _H26X_RATE,
                         "yuv420p", True),
             FastEncoder("libx265", ("-preset", "fast", "-crf", "20", "-tag:v", "hvc1",
                                     "-x265-params", "log-level=error") + _H26X_RATE, "yuv420p", True)),
}

_encoder_cache: dict[tuple, object] = {}


def _file_key(path: str) -> tuple:
    st = os.stat(path)
    return (os.path.abspath(path), st.st_mtime, st.st_size)


def encoders(ffmpeg: str, timeout: float = 10.0) -> set[str]:
    """Names of the encoders this ffmpeg lists. Cached per file."""
    key = ("list",) + _file_key(ffmpeg)
    if key not in _encoder_cache:
        out = _run([ffmpeg, "-hide_banner", "-encoders"], timeout).stdout.decode("utf-8", "replace")
        _encoder_cache[key] = {tok for line in out.splitlines() for tok in line.split()[1:2]}
    return _encoder_cache[key]  # type: ignore[return-value]


def encoder_works(ffmpeg: str, enc: FastEncoder, timeout: float = 20.0) -> bool:
    """Encode three tiny frames: a listed encoder may still fail to open
    (hardware encoders, missing libraries). Cached per file and encoder."""
    key = ("works", enc) + _file_key(ffmpeg)
    if key not in _encoder_cache:
        cmd = [ffmpeg, "-hide_banner", "-nostdin", "-v", "error",
               "-f", "lavfi", "-i", "color=c=gray:s=64x64:r=25", "-frames:v", "3",
               "-c:v", enc.name, *enc.args, "-pix_fmt", enc.pix_fmt, "-f", "null", "-"]
        try:
            ok = _run(cmd, timeout).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            ok = False
        _encoder_cache[key] = ok
    return bool(_encoder_cache[key])


def fast_encoder(ffmpeg: str, codec: str) -> FastEncoder | None:
    """The first working fast-engine encoder for a component codec key."""
    listed = encoders(ffmpeg)
    for enc in FAST_ENCODERS.get(codec, ()):
        if enc.name in listed and encoder_works(ffmpeg, enc):
            return enc
    return None


#: Raw frame formats encode_command accepts (ltcfilegen.picture.PIX_FMTS).
SOURCE_FORMATS = ("gray", "rgb24", "ya8", "rgba")


def encode_command(ffmpeg: str, enc: FastEncoder, w: int, h: int, src_fmt: str, rate: Fraction,
                   pcm: dict, out: str, timecode: str | None = None) -> list[str]:
    """Encode raw frames from stdin (8-bit ``src_fmt``: gray, rgb24, ya8 or
    rgba; top row first, full range) and add the PCM LTC track in one pass."""
    if src_fmt not in SOURCE_FORMATS:
        raise ValueError(f"unknown frame format {src_fmt!r}")
    acodec = {"s16le": "pcm_s16le", "s24le": "pcm_s24le"}[pcm["format"]]
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y",
           "-f", "rawvideo", "-pix_fmt", src_fmt, "-color_range", "pc",
           "-s", f"{w}x{h}", "-framerate", f"{rate.numerator}/{rate.denominator}", "-i", "pipe:0",
           "-f", pcm["format"], "-ar", str(pcm["sample_rate"]), "-ac", str(pcm["channels"]),
           "-i", pcm["path"],
           "-map", "0:v:0", "-map", "1:a:0"]
    if enc.yuv:
        # accurate_rnd: without it gray input gives V=127 for white (BT.709 wants 128).
        vf = f"scale=out_color_matrix=bt709:out_range=tv:flags=accurate_rnd+full_chroma_int,{COLOUR_TAGS}:range=tv"
    else:
        vf = f"format={enc.pix_fmt},{COLOUR_TAGS}"
    cmd += ["-vf", vf, "-c:v", enc.name, *enc.args, "-pix_fmt", enc.pix_fmt,
            "-c:a", acodec, "-metadata:s:a:0", "title=LTC", "-metadata:s:a:0", "handler_name=LTC"]
    if timecode:
        cmd += ["-timecode", timecode]
    cmd.append(out)
    return cmd


_python_cache: dict[str, str | None] = {}


def python_for_worker() -> str | None:
    """A Python that can import numpy, to run the fast worker outside
    TouchDesigner (TouchDesigner's own interpreter first). Cached."""
    if "py" not in _python_cache:
        found = None
        for cand in _python_candidates():
            try:
                if _run([cand, "-c", "import numpy"], 30).returncode == 0:
                    found = cand
                    break
            except Exception:
                continue
        _python_cache["py"] = found
    return _python_cache["py"]
