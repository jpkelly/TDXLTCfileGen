#!/usr/bin/env python3
"""Make the ready-to-open project (dist/LTCfileGen-<version>.toe).

Run after tools/td_release.py has written the self-contained td/LTCfileGen.tox:

    python3 tools/release_toe.py

It never opens TouchDesigner. With TouchDesigner's toeexpand / toecollapse
(as vCon's release does) it starts from TouchDesigner's own empty project
(Samples/Setup/Base/NewProject.toe), so nothing from the development machine
ships (start-file bookmarks, MIDI devices, stored data), and:

* puts the self-contained component in /project1;
* stamps the project with the component's TouchDesigner build;
* opens in Perform mode, showing the component's panel in its own window.

It checks the result by expanding it again. The release ships only this
.toe; TouchDesigner users who want the component take td/LTCfileGen.tox
from the repository.
"""

from __future__ import annotations

import argparse
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TOX = REPO / "td" / "LTCfileGen.tox"
COMP = "LTCfileGen"
PANEL_SIZE = (1100, 836)          # tools/td_build_ui.py PANEL_SIZE
TEMPLATES = ("/Applications/TouchDesigner.app/Contents/Resources/tfs/Samples/Setup/Base/NewProject.toe",
             r"C:\Program Files\Derivative\TouchDesigner\Samples\Setup\Base\NewProject.toe")
#: Must not appear anywhere in the shipped files (plus this machine's name).
#: The code itself names "Application Support" / LOCALAPPDATA, so those can't be on the list.
PRIVATE = [b"/Users/", b"\\Users\\", b"/home/", b"scratchpad", b"private/tmp", b"tdmcp", b"TDMCP"]
#: Known text from TouchDesigner itself, not from any user's machine: the
#: dropdown widget's pop-up menu is a live clone of /sys/TDTox/popMenu, whose
#: style template keeps a Derivative developer's path. Every TouchDesigner
#: install ships it, and the clone restores it on every copy.
ALLOWED = [b"C:/Users/sunsp/Desktop/Derivative/TDSystemII/DevIvan/~TdDevelopment.json"]


def find_tool(name: str) -> str:
    for cand in (f"/Applications/TouchDesigner.app/Contents/MacOS/{name}",
                 rf"C:\Program Files\Derivative\TouchDesigner\bin\{name}.exe"):
        if Path(cand).is_file():
            return cand
    found = shutil.which(name)
    if found:
        return found
    sys.exit(f"{name} not found (it ships with TouchDesigner)")


def toc_of(d: Path) -> Path:
    return d.with_name(d.name[: -len(".dir")] + ".toc")


def expand(src: Path, work: Path) -> Path:
    """Expand a .toe/.tox copy in work; returns its .dir (the .toc is beside it)."""
    work.mkdir(parents=True)
    dst = work / ("t" + src.suffix)
    shutil.copyfile(src, dst)
    # toeexpand exits 1 even when it succeeds (TD 2025.33230): check the result.
    proc = subprocess.run([find_tool("toeexpand"), dst.name], cwd=work, capture_output=True, text=True)
    out = work / (dst.name + ".dir")
    if not out.is_dir() or not toc_of(out).is_file():
        raise RuntimeError(f"toeexpand failed (exit {proc.returncode}): {(proc.stderr or proc.stdout).strip()}")
    return out


def entries(d: Path) -> list[str]:
    return [e for e in toc_of(d).read_text(encoding="utf-8").splitlines() if e and not e.startswith("#")]


def private_hits(d: Path) -> list[str]:
    words = PRIVATE + [socket.gethostname().split(".")[0].encode()]
    hits = set()
    for f in d.rglob("*"):
        if f.is_file():
            data = f.read_bytes()
            for allowed in ALLOWED:
                data = data.replace(allowed, b"")
            if any(w in data for w in words):
                hits.add(f.relative_to(d).as_posix())
    return sorted(hits)


def version() -> str:
    text = (REPO / "src" / "ltcfilegen" / "__init__.py").read_text(encoding="utf-8")
    return re.search(r'__version__\s*=\s*"([^"]+)"', text).group(1)


def build(template: Path, tox: Path, out: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        d = expand(template, work / "tpl")
        x = expand(tox, work / "tox")
        toc = entries(d)
        if any(e.startswith("project1/") for e in toc):
            raise RuntimeError(f"{template} is not an empty project")
        leaks = private_hits(x)
        if leaks:
            raise RuntimeError(f"private paths or names in the .tox: {', '.join(leaks)}")

        # 1. the component in /project1
        new = [e for e in entries(x) if not e.startswith(".")]
        if not new or not all(e.startswith(COMP + ".") or e.startswith(COMP + "/") for e in new):
            raise RuntimeError(f"{tox} does not hold a single {COMP} component")
        for e in new:
            dst = d / "project1" / e
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(x / e, dst)
        at = max(i for i, e in enumerate(toc) if e.startswith("project1.")) + 1
        toc[at:at] = ["project1/" + e for e in new]
        toc_of(d).write_text("".join(e + "\n" for e in toc), encoding="utf-8")

        # 2. the component's build, not the template's (2023): no upgrade pass on open
        shutil.copyfile(x / ".build", d / ".build")

        # 3. open in Perform mode on the panel
        app = d / ".application"
        text = app.read_text(encoding="utf-8")
        text, n = re.subn(r"(winplacement [^\n]*?perform\.path=\S+)(?: perform\.start=\d)?", r"\1 perform.start=1", text)
        if n != 1:
            raise RuntimeError("no winplacement line in .application")
        app.write_text(text, encoding="utf-8")
        parm = d / "perform.parm"
        lines = [l for l in parm.read_text(encoding="utf-8").splitlines()
                 if l.split(" ")[0] not in ("winop", "title", "size", "winw", "winh")]
        end = len(lines) - 1 - lines[::-1].index("?")
        lines[end:end] = [f"winop 0 /project1/{COMP}", 'title 16 "LTC File Generator" me', "size 0 custom",
                          f"winw 0 {PANEL_SIZE[0]}", f"winh 0 {PANEL_SIZE[1]}"]
        parm.write_text("\n".join(lines) + "\n", encoding="utf-8")

        (d.parent / "t.toe").unlink()
        subprocess.run([find_tool("toecollapse"), d.name], cwd=d.parent, check=True, capture_output=True)
        built = d.parent / "t.toe"

        # 4. check by expanding again
        c = expand(built, work / "check")
        top = sorted({e.split("/")[0].split(".")[0] for e in entries(c) if not e.startswith(".")})
        if top != ["perform", "project1"]:
            raise RuntimeError(f"top level is {top}")
        kids = sorted({e.split("/")[1].split(".")[0] for e in entries(c) if e.startswith("project1/")})
        if kids != [COMP]:
            raise RuntimeError(f"/project1 holds {kids}, expected only {COMP}")
        for e in new:
            if (c / "project1" / e).read_bytes() != (x / e).read_bytes():
                raise RuntimeError(f"project1/{e} differs from the .tox")
        if "perform.start=1" not in (c / ".application").read_text(encoding="utf-8"):
            raise RuntimeError("Perform mode on start was not kept")
        leaks = private_hits(c)
        if leaks:
            raise RuntimeError(f"private paths or names in: {', '.join(leaks)}")

        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(built, out)
        print(f"{len(new)} component entries in /project1; build "
              f"{(c / '.build').read_text().split()[3]}; Perform mode on start, "
              f"window {PANEL_SIZE[0]}x{PANEL_SIZE[1]}; no private paths or names")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tox", type=Path, default=TOX)
    ap.add_argument("--template", type=Path, help="empty TouchDesigner project (default: the installed NewProject.toe)")
    ap.add_argument("--out", type=Path, help="default: dist/LTCfileGen-<version>.toe")
    args = ap.parse_args()
    template = args.template or next((Path(t) for t in TEMPLATES if Path(t).is_file()), None)
    if template is None:
        sys.exit("TouchDesigner's Samples/Setup/Base/NewProject.toe not found: pass --template")
    out = args.out or REPO / "dist" / f"LTCfileGen-{version()}.toe"
    print(f"=== building {out.name} from {args.tox.name} ===")
    build(template, args.tox, out)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
