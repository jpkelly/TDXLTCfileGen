"""A render job: what to make, and everything derived from it per frame.

Start, duration and pre-roll are entered as timecode in LTC units, i.e. the
timecode a media server reads (frames 00-29 at 59.94 fps). The movie starts
at ``start - preroll`` and ends where ``start + duration`` begins.

* Duration is added clock-style: start 01:00:00;00 plus 01:00:00;00 ends at
  02:00:00;00 in drop-frame too. If the end label is skipped in drop-frame
  (e.g. 01:01:00;00), the file runs to the next real label (01:01:00;02).
* Pre-roll is subtracted clock-style and may cross midnight
  (start 00:00:00:00 with 10 s pre-roll starts at 23:59:50:00). A skipped
  drop-frame label snaps to the frame before it (earlier, never later).
  Like any drop-frame span, a pre-roll that crosses a dropping minute holds
  2 fewer frames than its label suggests (4 at 59.94 video frames).

Above 30 fps each LTC frame spans two video frames. The video-frame label is
always derived from the LTC label (frames * 2 + phase), never counted
separately, so the two can't disagree.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import timecode as tc


@dataclass(frozen=True)
class JobSpec:
    rate: str = "25"
    drop_frame: bool = False
    start: str = "01:00:00:00"
    duration: str = "00:01:00:00"
    preroll: str = "00:00:00:00"


def _parse_span(text: str, nominal: int, what: str) -> tuple[int, int, int, int]:
    """A duration-like label: may be a skipped drop-frame label, must be in range."""
    h, m, s, f = tc.parse_label(text)
    if not (0 <= h < 24 and 0 <= m < 60 and 0 <= s < 60):
        raise tc.TimecodeError(f"{what} {text!r} is out of range (max 23:59:59)")
    if not 0 <= f < nominal:
        raise tc.TimecodeError(f"{what} {text!r}: frames must be 00-{nominal - 1:02d}")
    return h, m, s, f


class Plan:
    """Validated job with per-frame lookups. Raises TimecodeError on bad input."""

    def __init__(self, spec: JobSpec):
        self.spec = spec
        self.rate = tc.get_rate(spec.rate)
        self.drop_frame = bool(spec.drop_frame)
        if self.drop_frame and not self.rate.drop_allowed:
            raise tc.TimecodeError(
                f"drop-frame is only used at 29.97 and 59.94 fps, not {self.rate.key}"
            )
        r = self.rate
        self.N = r.ltc_nominal                       # LTC frames per second
        self.D = tc.drop_count(self.N, self.drop_frame)
        self.div = r.ltc_div                          # video frames per LTC frame
        self.per_day = tc.frames_per_day(self.N, self.D)

        start = tc.check_label(spec.start, self.N, self.D, "start")
        dur = _parse_span(spec.duration, self.N, "duration")
        pre = _parse_span(spec.preroll, self.N, "pre-roll")

        self.start_abs = tc.label_to_frames(*start, self.N, self.D)

        days, *end = tc.add_labels(start, dur, self.N)
        h, m, s, f = end
        if not tc.label_exists(h, m, s, f, self.N, self.D):
            f = self.D                                # snap forward to first real label
        end_abs = tc.label_to_frames(h, m, s, f, self.N, self.D) + days * self.per_day
        self.main_frames = end_abs - self.start_abs
        if self.main_frames <= 0:
            raise tc.TimecodeError("duration must be longer than zero")

        pdays, *first = tc.sub_labels(start, pre, self.N)
        h, m, s, f = first
        if tc.label_exists(h, m, s, f, self.N, self.D):
            first_abs = tc.label_to_frames(h, m, s, f, self.N, self.D)
        else:                                         # snap back to the frame before
            first_abs = tc.label_to_frames(h, m, s, self.D, self.N, self.D) - 1
        first_abs += pdays * self.per_day
        self.preroll_frames = self.start_abs - first_abs
        self.first_abs = first_abs % self.per_day

        self.ltc_frames = self.preroll_frames + self.main_frames
        if self.ltc_frames > self.per_day:
            raise tc.TimecodeError("pre-roll plus duration must be under 24 hours")
        self.video_frames = self.ltc_frames * self.div

    # ------------------------------------------------------------------
    # Per-frame lookups (i = video frame index in the file, from 0)

    def ltc_abs(self, i: int) -> int:
        return (self.first_abs + i // self.div) % self.per_day

    def ltc_label(self, i: int) -> tuple[int, int, int, int]:
        return tc.frames_to_label(self.ltc_abs(i), self.N, self.D)

    def video_label(self, i: int) -> tuple[int, int, int, int]:
        h, m, s, f = self.ltc_label(i)
        return h, m, s, f * self.div + i % self.div

    def ltc_text(self, i: int) -> str:
        return tc.format_label(*self.ltc_label(i), drop=self.drop_frame)

    def video_text(self, i: int) -> str:
        return tc.format_label(*self.video_label(i), drop=self.drop_frame)

    def in_preroll(self, i: int) -> bool:
        return i < self.preroll_frames * self.div

    def remaining_text(self, i: int) -> str:
        """Time left until the end of the file, counted in LTC frames."""
        left = self.ltc_frames - i // self.div
        return tc.format_label(*tc.frames_to_label(left, self.N, self.D), drop=self.drop_frame)

    @property
    def embedded_timecode(self) -> str:
        """Start timecode for the movie's timecode track (video-frame units)."""
        return self.video_text(0)

    @property
    def end_text(self) -> str:
        """Label of the first frame after the file (start + duration)."""
        return tc.format_label(
            *tc.frames_to_label(self.start_abs + self.main_frames, self.N, self.D),
            drop=self.drop_frame,
        )

    # ------------------------------------------------------------------
    # Audio timing (exact integer arithmetic; no accumulated floats)

    def sample_at_video_frame(self, i: int, sr: int) -> int:
        """First audio sample of video frame i (rounded to nearest)."""
        num, den = self.rate.num, self.rate.den
        return (2 * i * sr * den + num) // (2 * num)

    def sample_range(self, i: int, sr: int) -> tuple[int, int]:
        return self.sample_at_video_frame(i, sr), self.sample_at_video_frame(i + 1, sr)

    def total_samples(self, sr: int) -> int:
        return self.sample_at_video_frame(self.video_frames, sr)

    def ltc_frame_samples(self, sr: int) -> float:
        """Length of one LTC frame in samples (may be fractional)."""
        return sr * self.rate.den * self.div / self.rate.num

    def summary(self) -> dict:
        return {
            "rate": self.rate.key,
            "drop_frame": self.drop_frame,
            "first": self.ltc_text(0),
            "start": tc.format_label(*tc.frames_to_label(self.start_abs, self.N, self.D), drop=self.drop_frame),
            "end": self.end_text,
            "preroll_ltc_frames": self.preroll_frames,
            "ltc_frames": self.ltc_frames,
            "video_frames": self.video_frames,
            "seconds": float(self.video_frames / self.rate.fps),
        }
