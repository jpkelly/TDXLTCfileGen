"""Frame rates and SMPTE timecode arithmetic.

Pure Python, no TouchDesigner imports, so it can be unit-tested anywhere.

Conventions
-----------
* A *label* is a timecode as people read it: (hours, minutes, seconds, frames).
* An *absolute frame* is the count of frames since 00:00:00:00 on the same
  counting scheme (non-drop or drop-frame). Labels and absolute frames
  convert both ways; absolute frames wrap at 24 hours.
* ``nominal`` is the integer frame count per timecode second (24, 25, 30,
  48, 50, 60). ``drop`` is how many frame *numbers* are skipped at the start
  of each minute that is not a multiple of ten: 2 at 29.97 fps, 4 at 59.94
  fps (SMPTE ST 12-1 / ANSI/SMPTE 12M-1986 section 5.2.2), 0 otherwise.

LTC (the audio code) can only count up to 30 frames per second. Above 30 fps
the LTC runs at half the video rate and each LTC frame spans two video
frames ("frame pairs"), which is what media servers read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction


@dataclass(frozen=True)
class Rate:
    """A video frame rate, kept as an exact fraction."""

    key: str          # menu key, e.g. "29.97"
    num: int
    den: int
    drop_allowed: bool

    @property
    def fps(self) -> Fraction:
        return Fraction(self.num, self.den)

    @property
    def nominal(self) -> int:
        """Frames per timecode second (24, 25, 30, 48, 50, 60)."""
        return round(self.num / self.den)

    @property
    def ltc_div(self) -> int:
        """Video frames per LTC frame: 2 above 30 fps, else 1."""
        return 2 if self.nominal > 30 else 1

    @property
    def ltc_nominal(self) -> int:
        """Frames per second counted by the LTC (24, 25 or 30)."""
        return self.nominal // self.ltc_div

    @property
    def ltc_fps(self) -> Fraction:
        return self.fps / self.ltc_div

    @property
    def is_25_family(self) -> bool:
        """25/50 fps use the EBU bit layout (polarity bit 59, not 27)."""
        return self.ltc_nominal == 25

    @property
    def td_fps(self) -> float:
        """Value for the Movie File Out TOP's FPS parameter.

        Measured on TD 2025.33230 (release) and 2025.33407 (beta): 23.976 /
        29.97 / 59.94 are written as the exact 24000/1001, 30000/1001,
        60000/1001. Passing the exact float (e.g. 29.97002997) gives odd
        rationals like 979001/32666, so the rounded menu value is the one to
        use. If the project runs at a 1001 rate, a whole-number movie FPS is
        written at the project's rate instead, so the component renders in a
        whole-number-FPS project (td/LtcGenExt.py render_cook_rate).
        """
        return float(self.key)


#: Supported rates, in menu order. 47.952 is left out on purpose: TD writes
#: it as 5994/125 rather than 48000/1001, which would drift against the LTC.
RATES: dict[str, Rate] = {
    r.key: r
    for r in (
        Rate("23.976", 24000, 1001, False),
        Rate("24", 24, 1, False),
        Rate("25", 25, 1, False),
        Rate("29.97", 30000, 1001, True),
        Rate("30", 30, 1, False),
        Rate("48", 48, 1, False),
        Rate("50", 50, 1, False),
        Rate("59.94", 60000, 1001, True),
        Rate("60", 60, 1, False),
    )
}


def get_rate(key: str) -> Rate:
    try:
        return RATES[str(key).strip()]
    except KeyError:
        raise ValueError(
            f"unsupported frame rate {key!r}; choose one of {', '.join(RATES)}"
        ) from None


class TimecodeError(ValueError):
    """A timecode that cannot be used, with a message meant for the user."""


def drop_count(nominal: int, drop_frame: bool) -> int:
    """Frame numbers skipped per (non-tenth) minute."""
    if not drop_frame:
        return 0
    if nominal not in (30, 60):
        raise TimecodeError("drop-frame exists only at 29.97 and 59.94 fps")
    return nominal // 15          # 2 at 30, 4 at 60


def frames_per_day(nominal: int, drop: int) -> int:
    return 24 * 6 * (600 * nominal - 9 * drop)


def label_to_frames(h: int, m: int, s: int, f: int, nominal: int, drop: int) -> int:
    """Absolute frame of a label. Does not validate; see :func:`check_label`."""
    total_minutes = 60 * h + m
    return ((3600 * h + 60 * m + s) * nominal + f) - drop * (total_minutes - total_minutes // 10)


def frames_to_label(n: int, nominal: int, drop: int) -> tuple[int, int, int, int]:
    """Label of an absolute frame (wraps at 24 h)."""
    n %= frames_per_day(nominal, drop)
    if drop:
        per_10min = 600 * nominal - 9 * drop
        per_min = 60 * nominal - drop
        tens, rem = divmod(n, per_10min)
        n += 9 * drop * tens
        if rem > drop:
            n += drop * ((rem - drop) // per_min)
    f = n % nominal
    s = (n // nominal) % 60
    m = (n // (60 * nominal)) % 60
    h = (n // (3600 * nominal)) % 24
    return h, m, s, f


def label_exists(h: int, m: int, s: int, f: int, nominal: int, drop: int) -> bool:
    if not (0 <= h < 24 and 0 <= m < 60 and 0 <= s < 60 and 0 <= f < nominal):
        return False
    if drop and s == 0 and f < drop and m % 10 != 0:
        return False
    return True


_TC_RE = re.compile(r"^\s*(\d{1,2})[:;.](\d{1,2})[:;.](\d{1,2})[:;.](\d{1,3})\s*$")


def parse_label(text: str) -> tuple[int, int, int, int]:
    """Parse ``HH:MM:SS:FF`` (``;`` or ``.`` also accepted as separators)."""
    m = _TC_RE.match(str(text))
    if not m:
        raise TimecodeError(f"{text!r} is not a timecode; use HH:MM:SS:FF")
    return tuple(int(g) for g in m.groups())  # type: ignore[return-value]


def format_label(h: int, m: int, s: int, f: int, drop: bool = False) -> str:
    """``HH:MM:SS:FF``, or ``HH:MM:SS;FF`` for drop-frame (SMPTE convention)."""
    sep = ";" if drop else ":"
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{f:02d}"


def check_label(text: str, nominal: int, drop: int, what: str = "timecode") -> tuple[int, int, int, int]:
    """Parse and validate a label that must exist on this counting scheme."""
    h, m, s, f = parse_label(text)
    if not (0 <= h < 24 and 0 <= m < 60 and 0 <= s < 60):
        raise TimecodeError(f"{what} {text!r} is out of range (max 23:59:59)")
    if not 0 <= f < nominal:
        raise TimecodeError(f"{what} {text!r}: frames must be 00-{nominal - 1:02d}")
    if not label_exists(h, m, s, f, nominal, drop):
        nxt = frames_to_label(label_to_frames(h, m, s, drop, nominal, drop), nominal, drop)
        raise TimecodeError(
            f"{what} {text!r} does not exist in drop-frame timecode "
            f"(frames 00-{drop - 1:02d} are skipped each minute except every tenth); "
            f"did you mean {format_label(*nxt, drop=True)}?"
        )
    return h, m, s, f


def add_labels(a: tuple[int, int, int, int], b: tuple[int, int, int, int], nominal: int) -> tuple[int, int, int, int, int]:
    """Clock-style addition of two labels with carries; returns (days, h, m, s, f)."""
    f = a[3] + b[3]
    s = a[2] + b[2] + f // nominal
    f %= nominal
    m = a[1] + b[1] + s // 60
    s %= 60
    h = a[0] + b[0] + m // 60
    m %= 60
    return h // 24, h % 24, m, s, f


def sub_labels(a: tuple[int, int, int, int], b: tuple[int, int, int, int], nominal: int) -> tuple[int, int, int, int, int]:
    """Clock-style subtraction ``a - b`` with borrows; returns (days, h, m, s, f) with days <= 0."""
    total_a = ((a[0] * 60 + a[1]) * 60 + a[2]) * nominal + a[3]
    total_b = ((b[0] * 60 + b[1]) * 60 + b[2]) * nominal + b[3]
    t = total_a - total_b
    days, t = divmod(t, 24 * 3600 * nominal)
    f = t % nominal
    t //= nominal
    return days, t // 3600, (t // 60) % 60, t % 60, f
