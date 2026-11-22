"""Find, validate, install and run ffmpeg.

TouchDesigner's movie writer records frame-exact video only, so the LTC
audio is added afterwards with ffmpeg (stream copy, no re-encode).

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


def python_for_pip() -> str | None:
    """A Python that can run pip, preferring TouchDesigner's own.

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
    for cand in cands:
        if cand and os.path.isfile(cand):
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
                retime: Fraction | None = None) -> list[str]:
    """Copy the video untouched and add the raw PCM LTC track.

    ``pcm`` is the dict returned by ``LtcEncoder.write_pcm``.

    ``retime`` rescales the video timestamps (still a stream copy) by
    ``written_rate / wanted_rate``. TouchDesigner 2025.33230 writes 60 fps
    files as 60000/1001; the frames are right, only the timestamps are
    labelled at the wrong rate, so ``retime=Fraction(1000, 1001)`` fixes them.
    pts, dts and durations are scaled separately so H.264/H.265 frame
    reordering survives.
    """
    codec = {"s16le": "pcm_s16le", "s24le": "pcm_s24le"}[pcm["format"]]
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y",
           "-i", video,
           "-f", pcm["format"], "-ar", str(pcm["sample_rate"]), "-ac", str(pcm["channels"]),
           "-i", pcm["path"],
           "-map", "0:v:0", "-map", "1:a:0",
           "-c:v", "copy", "-c:a", codec,
           "-metadata:s:a:0", "title=LTC", "-metadata:s:a:0", "handler_name=LTC"]
    if retime is not None and retime != 1:
        a, b = retime.numerator, retime.denominator
        cmd += ["-bsf:v", f"setts=pts=PTS*{a}/{b}:dts=DTS*{a}/{b}:duration=DURATION*{a}/{b}"]
    if timecode:
        cmd += ["-timecode", timecode]
    if progress_path:
        cmd += ["-progress", progress_path]
    cmd.append(out)
    return cmd
