"""The timecode picture, frame by frame, without TouchDesigner.

TouchDesigner draws the picture with Text TOPs and ``td/glsl/picture.glsl``.
The fast engine builds the same frames here with numpy, so it can feed an
encoder far faster than TouchDesigner's loop runs:

* :func:`frame_state` decides what frame *i* shows. TouchDesigner uses it
  too, so both engines show the same text, flash and marker.
* :class:`LineLayout` gives every character of a line a fixed position, so
  digits never shift sideways (both engines draw lines this way).
* :class:`TextLayer` holds one line of text (main readout, secondary line,
  countdown), built from ten reference renders made by TouchDesigner with
  every digit set to 0, then 1, ... 9. A frame's line is then assembled
  from per-digit cells. With fixed positions a digit's pixels do not depend
  on its neighbours; :meth:`TextLayer.from_refs` still rejects layouts where
  the digit cells touch or move, and the component compares assembled lines
  and whole frames with TouchDesigner's own before every fast render.
* :class:`Compositor` repeats the shader's arithmetic and redraws only the
  pixels that change from one frame to the next.

With ``cfg["alpha"]`` (HAP Alpha, ProRes 4444) the background is clear: each
pixel's alpha is the weight the opaque picture gives the text colour over
the background, and the colour is premultiplied (text colour x alpha), as
TouchDesigner plays such files back. Flash frames and the debug barcode stay
opaque.

Arrays are top row first (as encoders expect). TouchDesigner's
``numpyArray()`` is bottom row first: flip it before building layers.
"""

from __future__ import annotations

import numpy as np

#: Weight of the info caption over the picture (glsl uInfo.x).
INFO_WEIGHT = 0.7
#: Marker slots that are not the current frame: mix(bg, fg, this).
MARKER_DIM = 0.2
#: Text lines the compositor knows, in the shader's input order.
LINES = ("main", "sub", "count")


class PictureError(ValueError):
    """The fast picture cannot match TouchDesigner's (message for the user)."""


def frame_state(plan, cfg: dict, i: int, preview: bool = False):
    """What video frame ``i`` shows: ``(texts, flash, slot)``.

    ``texts`` maps each of :data:`LINES` to its string ('' = not shown).
    ``flash`` swaps background and text colours on the first frame of each
    second (never in the idle preview). ``slot`` is the frame number within
    the second, which the frame marker lights.
    """
    ltc_text, video_text = plan.ltc_text(i), plan.video_text(i)
    high = plan.div == 2
    main_is_video = high and cfg["main"] == "video"
    sub = ""
    if high and cfg["secondary"]:
        sub = f"LTC {ltc_text}" if main_is_video else f"VIDEO {video_text}"
    texts = {
        "main": video_text if main_is_video else ltc_text,
        "sub": sub,
        "count": f"-{plan.remaining_text(i)}" if cfg["countdown"] else "",
    }
    slot = plan.video_label(i)[3]
    flash = bool(cfg["flash"]) and slot == 0 and not preview
    return texts, flash, slot


def template(text: str) -> str:
    """The text with every digit replaced by '#'."""
    return "".join("#" if c.isdigit() else c for c in text)


def with_digit(tmpl: str, d: int) -> str:
    return tmpl.replace("#", str(d))


#: Digit sequences for checking assembled lines: every digit, both orders,
#: and runs of 1s (the pair Arial kerns).
CHECK_DIGITS = ("1234567890", "9876543210", "1011010110", "4741144717")


def check_strings(tmpl: str) -> list[str]:
    """Strings on the layout ``tmpl`` to compare with real renders."""
    out = []
    for seq in CHECK_DIGITS:
        k = iter(seq * (len(tmpl) // len(seq) + 1))
        out.append("".join(next(k) if c == "#" else c for c in tmpl))
    return out


def check_frames(plan, cfg: dict) -> list[int]:
    """Frames whose whole picture the component compares before a fast render:
    the first two, the last, and the first flash frame and the one after."""
    n = plan.video_frames
    frames = {0, min(1, n - 1), n - 1}
    if cfg.get("flash"):
        for i in range(1, min(n, plan.rate.nominal * plan.div + 1)):
            if plan.video_label(i)[3] == 0:
                frames |= {i, min(i + 1, n - 1)}
                break
    return sorted(frames)


class LineLayout:
    """Fixed character positions for a text line, so digits never move.

    A plain centred string shifts whenever its width changes: Arial kerns
    "11", so ``01:11:11:11`` is narrower than ``01:00:00:00`` and the whole
    readout jumps sideways. Here every digit gets a cell as wide as the
    widest digit (narrower digits are centred in it) and the text between
    digits keeps its own width. TouchDesigner draws the line from these
    positions (Text TOP Specification DAT: x = pen position, y = baseline),
    and the fast engine relies on them to assemble frames from digit cells.

    ``advance(s)`` gives the pen advance of ``s`` in pixels (the Text TOP's
    ``evalTextSize(s)[0]``).
    """

    def __init__(self, tmpl: str, advance, center_x: float, baseline: float):
        self.template = tmpl
        adv = [float(advance(str(d))) for d in range(10)]
        pitch = max(adv)
        self.offsets = [(pitch - a) / 2.0 for a in adv]
        parts, run = [], ""
        for ch in tmpl:
            if ch == "#":
                if run:
                    parts.append((run, float(advance(run))))
                    run = ""
                parts.append(("#", pitch))
            else:
                run += ch
        if run:
            parts.append((run, float(advance(run))))
        x = center_x - sum(wd for _, wd in parts) / 2.0
        self.parts = []                     # (pen x, '#' or static text)
        for text, wd in parts:
            self.parts.append((x, text))
            x += wd
        self.y = int(round(baseline))

    def rows(self, text: str) -> list[tuple[int, int, str]]:
        """Specification DAT rows (x, y, text) drawing ``text``."""
        if template(text) != self.template:
            raise PictureError(f"{text!r} does not fit the layout {self.template!r}")
        digits = iter(c for c in text if c.isdigit())
        out = []
        for x, part in self.parts:
            if part == "#":
                d = next(digits)
                out.append((int(round(x + self.offsets[int(d)])), self.y, d))
            else:
                out.append((int(round(x)), self.y, part))
        return out


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """[start, end) of each run of True in a 1-D mask."""
    m = np.concatenate(([False], mask.astype(bool), [False]))
    edges = np.flatnonzero(m[1:] != m[:-1])
    return list(zip(edges[0::2].tolist(), edges[1::2].tolist()))


class TextLayer:
    """One text line, assembled per frame from per-digit cells.

    ``base`` is the line's band of rows (y0..y1, full width) as rendered
    with every digit 0; ``cells[k]`` is the column range of digit slot k;
    ``patches[d][k]`` is slot k's pixels when it shows digit d.
    """

    def __init__(self, tmpl: str, y0: int, base: np.ndarray, cells, patches):
        self.template = tmpl
        self.slots = [k for k, c in enumerate(tmpl) if c == "#"]
        self.y0, self.base, self.cells, self.patches = y0, base, cells, patches
        self.y1 = y0 + base.shape[0]

    @classmethod
    def from_refs(cls, tmpl: str, refs: list[np.ndarray]) -> "TextLayer | None":
        """Build from ten uint8 alpha frames: ``tmpl`` rendered with digit 0..9.

        Returns None for a line with no visible pixels.
        """
        if len(refs) != 10:
            raise ValueError("need ten reference renders, digits 0-9")
        stack = np.stack(refs)                      # (10, h, w)
        ink_rows = np.flatnonzero(stack.any(axis=(0, 2)))
        if ink_rows.size == 0:
            return None
        y0, y1 = int(ink_rows[0]), int(ink_rows[-1]) + 1
        band = stack[:, y0:y1, :]
        varying = (band != band[0]).any(axis=(0, 1))
        cells = _runs(varying)
        n = tmpl.count("#")
        if len(cells) != n:
            raise PictureError(
                f"the font's digits change position or touch ({len(cells)} digit cells "
                f"for {n} digits in {tmpl!r}); use a font with fixed-width digits")
        patches = [[band[d][:, a:b].copy() for a, b in cells] for d in range(10)]
        return cls(tmpl, y0, band[0].copy(), cells, patches)

    def render(self, text: str, h: int, w: int) -> np.ndarray:
        """The full-frame alpha of ``text`` (for checks; frames use cells)."""
        out = np.zeros((h, w), np.uint8)
        band = self.base.copy()
        for k, d in enumerate(self.digits(text)):
            a, b = self.cells[k]
            band[:, a:b] = self.patches[d][k]
        out[self.y0:self.y1] = band
        return out

    def digits(self, text: str) -> list[int]:
        if len(text) != len(self.template):
            raise PictureError(f"{text!r} does not fit the layout {self.template!r}")
        return [ord(text[p]) - 48 for p in self.slots]

    # Saved as plain arrays so the fast worker can load them without pickle.
    def to_arrays(self, prefix: str) -> dict:
        arrays = {f"{prefix}.base": self.base,
                  f"{prefix}.meta": np.array([self.y0], np.int64),
                  f"{prefix}.cells": np.array(self.cells, np.int64).reshape(-1, 2),
                  f"{prefix}.template": np.frombuffer(self.template.encode("utf-8"), np.uint8)}
        for d in range(10):
            for k in range(len(self.cells)):
                arrays[f"{prefix}.p{d}.{k}"] = self.patches[d][k]
        return arrays

    @classmethod
    def from_arrays(cls, arrays, prefix: str) -> "TextLayer | None":
        if f"{prefix}.base" not in arrays:
            return None
        tmpl = bytes(arrays[f"{prefix}.template"]).decode("utf-8")
        cells = [tuple(int(v) for v in c) for c in arrays[f"{prefix}.cells"]]
        patches = [[arrays[f"{prefix}.p{d}.{k}"] for k in range(len(cells))] for d in range(10)]
        return cls(tmpl, int(arrays[f"{prefix}.meta"][0]), arrays[f"{prefix}.base"], cells, patches)


def is_gray(cfg: dict) -> bool:
    """True when every colour in the picture is a gray (one channel is enough)."""
    return all(len(set(cfg[k])) == 1 for k in ("fg", "bg"))


def _q(x: np.ndarray) -> np.ndarray:
    """Float colour to 8 bits, as the GPU stores it."""
    return np.clip(np.floor(np.asarray(x, np.float32) * np.float32(255) + np.float32(0.5)),
                   0, 255).astype(np.uint8)


def _mix(x, y, a):
    """GLSL mix() in float32."""
    x, y, a = (np.asarray(v, np.float32) for v in (x, y, a))
    return x * (np.float32(1) - a) + y * a


#: Raw frame format by (gray, alpha), as ffmpeg names it.
PIX_FMTS = {(True, False): "gray", (False, False): "rgb24", (True, True): "ya8", (False, True): "rgba"}


class Compositor:
    """Frames of the timecode picture, as the shader draws them.

    ``layers`` maps names in :data:`LINES` to :class:`TextLayer` or None;
    ``info`` is the caption's full-frame uint8 alpha or None. ``cfg`` needs
    w, h, fg, bg, marker, marker_h, barcode and strip_h, plus ``slots`` (frames
    per second shown by the marker) and optionally ``alpha``.

    Frames are gray, RGB, gray + alpha or RGBA (:attr:`pix_fmt`).
    """

    def __init__(self, cfg: dict, layers: dict, info: np.ndarray | None = None):
        self.w, self.h = int(cfg["w"]), int(cfg["h"])
        self.gray = is_gray(cfg)
        self.alpha = bool(cfg.get("alpha"))
        self.ch = 1 if self.gray else 3                 # colour channels
        self.pix_fmt = PIX_FMTS[(self.gray, self.alpha)]
        fg = np.array(cfg["fg"], np.float32)[: self.ch]
        bg = np.array(cfg["bg"], np.float32)[: self.ch]
        self.layers = {k: v for k, v in layers.items() if v is not None}
        shape = (self.h, self.w)
        self.canvas = {k: np.zeros(shape, np.uint8) for k in self.layers}
        self.info = info if info is not None else np.zeros(shape, np.uint8)
        if self.info.shape != shape:
            raise PictureError(f"caption is {self.info.shape[1]}x{self.info.shape[0]}, "
                               f"expected {self.w}x{self.h}")
        for name, layer in self.layers.items():
            if layer.base.shape[1] != self.w or layer.y1 > self.h:
                raise PictureError(f"the {name} line does not fit {self.w}x{self.h}")

        # Colour of every (text alpha, caption alpha) pair, normal and flash.
        a = (np.arange(256, dtype=np.float32) / np.float32(255))[:, None, None]
        ai = (np.arange(256, dtype=np.float32) / np.float32(255))[None, :, None]
        self.lut, self.marker_on, self.marker_dim = {}, {}, {}
        for flash in (False, True):
            b, f = (fg, bg) if flash else (bg, fg)
            if self.alpha and not flash:
                # Clear background: alpha = the text colour's weight, colour premultiplied.
                t = _mix(np.float32(0), np.float32(1), a)
                t = _mix(t, np.float32(1), ai * np.float32(INFO_WEIGHT))
                self.lut[flash] = np.concatenate([_q(f * t), _q(t)], axis=-1)
                self.marker_on[flash] = np.concatenate([_q(f), [255]]).astype(np.uint8)
                dim = np.float32(MARKER_DIM)
                self.marker_dim[flash] = np.concatenate([_q(f * dim), _q([dim])]).astype(np.uint8)
                continue
            col = _mix(b, f, a)
            col = _mix(col, f, ai * np.float32(INFO_WEIGHT))
            on, dim = _q(f), _q(_mix(b, f, MARKER_DIM))
            if self.alpha:                                  # flash frames stay opaque
                col = np.concatenate([col, np.ones(col.shape[:2] + (1,), np.float32)], axis=-1)
                on, dim = (np.concatenate([v, [255]]).astype(np.uint8) for v in (on, dim))
            self.lut[flash] = _q(col)                       # (256, 256, channels)
            self.marker_on[flash] = on
            self.marker_dim[flash] = dim

        # Frame marker geometry (shader: px = pixel centre, y from the bottom).
        self.marker = bool(cfg.get("marker"))
        mh = int(cfg.get("marker_h", 0))
        self.band = (self.h - mh, self.h)
        self.inner_rows = (max(0, self.h - mh + 2), max(0, self.h - 2))
        slots = int(cfg.get("slots", 1))
        px = np.arange(self.w, dtype=np.float32) + np.float32(0.5)
        slot_w = np.float32(self.w) / np.float32(slots)
        s = np.floor(px / slot_w)
        fx = px - s * slot_w
        self.slot_of_col = s.astype(np.int64)
        inner = (fx >= 1) & (fx < slot_w - 1)
        self.slot_cols = {}
        for k in range(slots):
            cols = np.flatnonzero(inner & (self.slot_of_col == k))
            if cols.size:
                self.slot_cols[k] = (int(cols[0]), int(cols[-1]) + 1)

        # Debug barcode: 24 bars across the top; upper half bits, lower inverted.
        self.barcode = bool(cfg.get("barcode"))
        sh = int(cfg.get("strip_h", 0))
        self.strip = (0, min(sh, self.h))
        self.strip_mid = sh // 2
        uv = (np.arange(self.w, dtype=np.float32) + np.float32(0.5)) / np.float32(self.w)
        bar = np.floor(uv * np.float32(24)).astype(np.int64)
        self.bar_cols = [tuple(int(v) for v in (np.flatnonzero(bar == k)[[0, -1]] + [0, 1]))
                         if (bar == k).any() else (0, 0) for k in range(24)]

        self.out = np.zeros(shape + (self.ch + self.alpha,), np.uint8)
        self.prev = None            # (digits per line, flash, slot, i) of the last frame

    # ------------------------------------------------------------------

    def frame(self, i: int, texts: dict, flash: bool, slot: int) -> np.ndarray:
        """The picture of frame ``i``. Returns the internal buffer (overwritten
        by the next call): write it out before asking for another frame."""
        digits = {k: self.layers[k].digits(texts[k]) for k in self.layers}
        p = self.prev
        if p is None or p[1] != flash:
            self._full(digits, flash, slot, i)
        else:
            rects = []
            for name, ds in digits.items():
                layer, canvas, old = self.layers[name], self.canvas[name], p[0][name]
                for k, (d, o) in enumerate(zip(ds, old)):
                    if d != o:
                        a, b = layer.cells[k]
                        canvas[layer.y0:layer.y1, a:b] = layer.patches[d][k]
                        rects.append((layer.y0, layer.y1, a, b))
            if self.marker and slot != p[2]:
                for s in (p[2], slot):
                    if s in self.slot_cols:
                        rects.append(self.inner_rows + self.slot_cols[s])
            if self.barcode:
                changed = (i ^ p[3]) & 0xFFFFFF
                for k in range(24):
                    if changed >> k & 1:
                        rects.append(self.strip + self.bar_cols[k])
            for r in rects:
                self._draw(r, flash, slot, i)
        self.prev = (digits, flash, slot, i)
        return self.out[..., 0] if self.out.shape[2] == 1 else self.out

    def _full(self, digits, flash, slot, i):
        for name, ds in digits.items():
            layer, canvas = self.layers[name], self.canvas[name]
            band = layer.base.copy()
            for k, d in enumerate(ds):
                a, b = layer.cells[k]
                band[:, a:b] = layer.patches[d][k]
            canvas[:] = 0
            canvas[layer.y0:layer.y1] = band
        self._draw((0, self.h, 0, self.w), flash, slot, i)

    def _draw(self, rect, flash, slot, i):
        y0, y1, x0, x1 = rect
        if y1 <= y0 or x1 <= x0:
            return
        a = None
        for canvas in self.canvas.values():
            a = canvas[y0:y1, x0:x1] if a is None else np.maximum(a, canvas[y0:y1, x0:x1])
        if a is None:
            a = np.zeros((y1 - y0, x1 - x0), np.uint8)
        self.out[y0:y1, x0:x1] = self.lut[flash][a, self.info[y0:y1, x0:x1]]
        if self.marker:
            self._draw_marker(rect, flash, slot)
        if self.barcode:
            self._draw_barcode(rect, i)

    def _draw_marker(self, rect, flash, slot):
        y0, y1, x0, x1 = rect
        r0, r1 = max(y0, self.inner_rows[0]), min(y1, self.inner_rows[1])
        if r1 <= r0:
            return
        for s, (a, b) in self.slot_cols.items():
            c0, c1 = max(x0, a), min(x1, b)
            if c1 > c0:
                self.out[r0:r1, c0:c1] = self.marker_on[flash] if s == slot else self.marker_dim[flash]

    def _draw_barcode(self, rect, i):
        y0, y1, x0, x1 = rect
        s0, s1 = max(y0, self.strip[0]), min(y1, self.strip[1])
        if s1 <= s0:
            return
        f = int(i)
        for k, (a, b) in enumerate(self.bar_cols):
            c0, c1 = max(x0, a), min(x1, b)
            if c1 <= c0:
                continue
            bit = (f >> k) & 1
            for r0, r1, v in ((s0, min(s1, self.strip_mid), bit), (max(s0, self.strip_mid), s1, 1 - bit)):
                if r1 > r0:
                    self.out[r0:r1, c0:c1, :self.ch] = 255 * v
                    self.out[r0:r1, c0:c1, self.ch:] = 255      # opaque


def save_layers(path: str, layers: dict, info: np.ndarray | None) -> None:
    """Write the glyph data the fast worker needs (``.npz``, no pickle)."""
    arrays = {}
    for name, layer in layers.items():
        if layer is not None:
            arrays.update(layer.to_arrays(name))
    if info is not None:
        ys, xs = np.nonzero(info)
        if ys.size:
            y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
            arrays["info.crop"] = info[y0:y1, x0:x1]
            arrays["info.at"] = np.array([y0, x0, info.shape[0], info.shape[1]], np.int64)
    np.savez(path, **arrays)


def load_layers(path: str):
    """Inverse of :func:`save_layers`: ``(layers, info)``."""
    with np.load(path, allow_pickle=False) as z:
        arrays = {k: z[k] for k in z.files}
    layers = {name: TextLayer.from_arrays(arrays, name) for name in LINES}
    info = None
    if "info.crop" in arrays:
        y0, x0, h, w = (int(v) for v in arrays["info.at"])
        crop = arrays["info.crop"]
        info = np.zeros((h, w), np.uint8)
        info[y0:y0 + crop.shape[0], x0:x0 + crop.shape[1]] = crop
    return layers, info
