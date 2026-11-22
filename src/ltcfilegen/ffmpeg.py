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
from dataclasses import dataclass, field

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


def candidates(override: str | None = None, env: dict | None = None) -> list[tuple[str, str]]:
    """(source, path) pairs in the order they are tried. Paths may not exist."""
    env = os.environ if env is None else env
    home = os.path.expanduser("~")
    out: list[tuple[str, str]] = []
    if override:
        out.append(("setting", override))
    if env.get(ENV_VAR):
        out.append((ENV_VAR, env[ENV_VAR]))
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


def find_ffmpeg(override: str | None = None, env: dict | None = None) -> tuple[FfmpegInfo | None, list[FfmpegInfo]]:
    """First usable ffmpeg, plus every candidate tried (for the setup dialog).

    An explicit setting that fails is reported, not silently replaced.
    """
    tried: list[FfmpegInfo] = []
    for src, path in candidates(override, env):
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
    """ffprobe next to the chosen ffmpeg, else on PATH or a standard location."""
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
# Mux


def mux_command(ffmpeg: str, video: str, pcm: dict, out: str,
                timecode: str | None = None, progress_path: str | None = None) -> list[str]:
    """Copy the video untouched and add the raw PCM LTC track.

    ``pcm`` is the dict returned by ``LtcEncoder.write_pcm``.
    """
    codec = {"s16le": "pcm_s16le", "s24le": "pcm_s24le"}[pcm["format"]]
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y",
           "-i", video,
           "-f", pcm["format"], "-ar", str(pcm["sample_rate"]), "-ac", str(pcm["channels"]),
           "-i", pcm["path"],
           "-map", "0:v:0", "-map", "1:a:0",
           "-c:v", "copy", "-c:a", codec,
           "-metadata:s:a:0", "title=LTC", "-metadata:s:a:0", "handler_name=LTC"]
    if timecode:
        cmd += ["-timecode", timecode]
    if progress_path:
        cmd += ["-progress", progress_path]
    cmd.append(out)
    return cmd
