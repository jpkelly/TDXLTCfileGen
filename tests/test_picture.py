"""The fast engine's picture: layout, digit cells and the shader arithmetic.

TouchDesigner is not available here, so glyphs come from a small fake font
(each digit a distinct bit pattern). The real comparison with
TouchDesigner's frames runs in the component before every fast render
(LtcGenExt.FastGlyphs).
"""

import numpy as np
import pytest

from ltcfilegen import picture as pic
from ltcfilegen.job import JobSpec, Plan

W, H = 320, 180


# ----------------------------------------------------------------------
# A fake font: 8x12 glyphs at fixed pen positions, alpha 0-255

def _glyph(ch: str) -> np.ndarray:
    g = np.zeros((12, 8), np.uint8)
    if ch.isdigit():
        d = int(ch)
        for k in range(4):                     # four bits as bars
            if (d + 1) >> k & 1:
                g[1:11, 1 + k * 2] = 255
        g[11, 1:1 + d % 7] = 128               # antialias-like edge
    elif ch in ":;":
        g[3:5, 3:5] = 255
        g[8:10, 3:5] = 255 if ch == ":" else 90
    elif ch != " ":
        g[2:10, 2:6] = 40 + ord(ch) % 200      # any other character: a block
    return g


def _advance(s: str) -> float:
    return sum(5.0 if c in ":; " else 10.0 for c in s)


def _draw(rows, h=H, w=W) -> np.ndarray:
    """Render Specification-DAT rows (x = pen, y = baseline from the bottom)."""
    out = np.zeros((h, w), np.uint8)
    for x, y, text in rows:
        pen = x
        for ch in text:
            g = _glyph(ch)
            top = h - 1 - y - g.shape[0] + 1
            out[top:top + 12, pen:pen + 8] = np.maximum(out[top:top + 12, pen:pen + 8], g)
            pen += int(_advance(ch))
    return out


def _layer(tmpl, cx=W / 2, baseline=H / 2):
    lay = pic.LineLayout(tmpl, _advance, cx, baseline)
    refs = [_draw(lay.rows(pic.with_digit(tmpl, d))) for d in range(10)]
    return lay, pic.TextLayer.from_refs(tmpl, refs)


# ----------------------------------------------------------------------

def test_layout_gives_every_digit_a_fixed_cell():
    def kerned(s):                              # "11" narrower, like Arial
        return _advance(s) - 3.0 * s.count("11")
    lay = pic.LineLayout("##:##", kerned, 100.0, 50.0)
    a = [r[0] for r in lay.rows("00:00")]
    b = [r[0] for r in lay.rows("11:11")]
    assert a == b                               # no sideways jump
    assert lay.rows("12:34")[2] == (98, 50, ":")      # 100 - 45/2 + 20 = 97.5
    assert lay.y == 50


def test_proportional_digits_are_centred_in_their_cell():
    adv = lambda s: sum(6.0 if c == "1" else 10.0 for c in s)
    lay = pic.LineLayout("#", adv, 50.0, 0.0)
    (x0, _, _), = lay.rows("0")
    (x1, _, _), = lay.rows("1")
    assert x1 - x0 == 2                         # (10 - 6) / 2


def test_layout_rejects_other_shapes():
    lay = pic.LineLayout("##:##", _advance, 100, 50)
    with pytest.raises(pic.PictureError):
        lay.rows("1:23:45")


@pytest.mark.parametrize("tmpl", ["##:##:##:##", "##:##:##;##", "LTC ##:##:##:##", "-##:##:##:##"])
def test_assembled_lines_equal_whole_renders(tmpl):
    lay, layer = _layer(tmpl)
    for s in pic.check_strings(tmpl) + [pic.with_digit(tmpl, 8)]:
        assert np.array_equal(layer.render(s, H, W), _draw(lay.rows(s))), s


def test_moving_glyphs_are_refused():
    """A centred plain string (no fixed cells) moves when its width changes."""
    tmpl = "##:##"
    def centred(s):
        x = int(W / 2 - (_advance(s) - 3 * s.count("11")) / 2)
        return _draw([(x, 90, s)])
    refs = [centred(pic.with_digit(tmpl, d)) for d in range(10)]
    with pytest.raises(pic.PictureError):
        pic.TextLayer.from_refs(tmpl, refs)


def test_check_strings_use_every_digit_and_runs_of_ones():
    s = pic.check_strings("##:##:##:##")
    assert all(len(x) == 11 for x in s)
    assert set("".join(s)) >= set("0123456789:")
    assert any("11" in x for x in s)


# ----------------------------------------------------------------------
# Frames

def _setup(rate="59.94", df=True, start="00:59:58;00", colour=False, **opts):
    plan = Plan(JobSpec(rate=rate, drop_frame=df, start=start, duration="00:00:04;00" if df else "00:00:04:00",
                        preroll="00:00:01:00"))
    cfg = dict(w=W, h=H, main="video", secondary=True, countdown=True, flash=True, marker=True,
               barcode=True, info=True, marker_h=8, strip_h=16, slots=plan.rate.nominal,
               fg=(1.0, 0.8, 0.1) if colour else (1.0, 1.0, 1.0),
               bg=(0.05, 0.1, 0.3) if colour else (0.0, 0.0, 0.0))
    cfg.update(opts)
    texts, _, _ = pic.frame_state(plan, cfg, 0)
    layers, lays = {}, {}
    for line, y in (("main", 90), ("sub", 60), ("count", 120)):
        if texts[line]:
            lays[line], layers[line] = _layer(pic.template(texts[line]), baseline=y)
        else:
            layers[line] = None
    info = np.zeros((H, W), np.uint8)
    info[150:160, 10:120] = np.arange(110, dtype=np.uint8)[None, :] * 2     # graded caption
    return plan, cfg, layers, (info if cfg["info"] else None), lays


def _shader(plan, cfg, layers, info, i):
    """td/glsl/picture.glsl, pixel for pixel, the plain way (whole frame)."""
    texts, flash, slot = pic.frame_state(plan, cfg, i)
    a = np.zeros((H, W), np.float32)
    for name, layer in layers.items():
        if layer is not None:
            a = np.maximum(a, layer.render(texts[name], H, W).astype(np.float32) / 255)
    bg, fg = np.float32(cfg["bg"]), np.float32(cfg["fg"])
    if flash:
        bg, fg = fg, bg
    clear = cfg.get("alpha") and not flash
    col = bg * (1 - a[..., None]) + fg * a[..., None]
    t = a[..., None]                                   # text weight = alpha when clear
    if info is not None:
        ai = info.astype(np.float32)[..., None] / 255 * np.float32(0.7)
        col = col * (1 - ai) + fg * ai
        t = t * (1 - ai) + ai
    yb = (H - 1 - np.arange(H, dtype=np.float32))[:, None] + np.float32(0.5)   # px.y from the bottom
    px = np.arange(W, dtype=np.float32)[None, :] + np.float32(0.5)
    if cfg["marker"]:
        slot_w = np.float32(W) / np.float32(cfg["slots"])
        s = np.floor(px / slot_w)
        fx = px - s * slot_w
        inner = (fx >= 1) & (fx < slot_w - 1) & (yb >= 2) & (yb < cfg["marker_h"] - 2) & (yb < cfg["marker_h"])
        on = inner & (s == slot)
        dim = inner & (s != slot)
        col = np.where(on[..., None], fg, col)
        col = np.where(dim[..., None], bg * np.float32(0.8) + fg * np.float32(0.2), col)
        t = np.where(on[..., None], np.float32(1), t)
        t = np.where(dim[..., None], np.float32(0.2), t)
    if cfg.get("alpha"):
        col = np.concatenate([fg * t, t] if clear else [col, np.ones_like(t)], axis=-1)
    if cfg["barcode"]:
        strip = yb >= H - cfg["strip_h"]
        bar = np.floor(px / np.float32(W) * 24).astype(np.int64)
        bit = ((i >> bar) & 1).astype(np.float32)
        upper = yb >= H - 0.5 * cfg["strip_h"]
        v = np.where(upper, bit, 1 - bit)[..., None]
        if cfg.get("alpha"):
            v = np.concatenate([v, v, v, np.ones_like(v)], axis=-1)
        col = np.where(strip[..., None], np.broadcast_to(v, col.shape), col)
    out = np.clip(np.floor(col * 255 + 0.5), 0, 255).astype(np.uint8)
    return out


def _as_shader(got, comp):
    """Compositor output (gray, RGB, gray + alpha or RGBA) as the shader's RGB(A)."""
    if got.ndim == 2:
        got = got[..., None]
    if comp.gray:
        got = np.concatenate([got[..., :1]] * 3 + [got[..., 1:]], axis=-1)
    return got


@pytest.mark.parametrize("colour", [False, True])
@pytest.mark.parametrize("alpha", [False, True])
def test_compositor_matches_the_shader(colour, alpha):
    plan, cfg, layers, info, _ = _setup(colour=colour, alpha=alpha)
    comp = pic.Compositor(cfg, layers, info)
    for i in (0, 1, 59, 60, 61, 119, 120, plan.video_frames - 1):
        texts, flash, slot = pic.frame_state(plan, cfg, i)
        got = _as_shader(pic.Compositor(cfg, layers, info).frame(i, texts, flash, slot), comp)
        want = _shader(plan, cfg, layers, info, i)
        assert got.shape == want.shape
        assert int(np.abs(got.astype(np.int16) - want).max()) <= 1, i


@pytest.mark.parametrize("colour", [False, True])
def test_clear_background_over_the_background_colour_is_the_opaque_picture(colour):
    """Premultiplied: colour + background x (1 - alpha) gives back the opaque frame."""
    plan, cfg, layers, info, _ = _setup(colour=colour, barcode=False)
    clear = pic.Compositor(dict(cfg, alpha=True), layers, info)
    solid = pic.Compositor(cfg, layers, info)
    bg = np.float32(cfg["bg"]) * 255
    for i in (1, 2, 61, plan.video_frames - 1):
        st = pic.frame_state(plan, cfg, i)
        assert not st[1]
        c = _as_shader(clear.frame(i, *st), clear).astype(np.float32)
        over = c[..., :3] + bg * (1 - c[..., 3:] / 255)
        want = _as_shader(solid.frame(i, *st), solid).astype(np.float32)
        assert np.abs(over - want).max() <= 2, i


def test_clear_frames_are_clear_except_flash_and_barcode():
    plan, cfg, layers, info, _ = _setup(alpha=True)
    comp = pic.Compositor(cfg, layers, info)
    flash_i = next(i for i in range(1, 200) if pic.frame_state(plan, cfg, i)[1])
    f = comp.frame(flash_i, *pic.frame_state(plan, cfg, flash_i)).copy()
    assert f.shape == (H, W, 2) and (f[..., 1] == 255).all()
    f = comp.frame(flash_i + 1, *pic.frame_state(plan, cfg, flash_i + 1))
    body = f[cfg["strip_h"]:]                          # below the barcode
    assert body[..., 1].min() == 0 and body[..., 1].max() == 255
    assert (body[..., 0] <= body[..., 1]).all()         # premultiplied white
    assert (f[:cfg["strip_h"], :, 1] == 255).all()      # the barcode is opaque
    dim = f[H - cfg["marker_h"] // 2, :, 1]
    assert 51 in dim                                    # unlit marker slots at 20 %


@pytest.mark.parametrize("opts", [{}, {"colour": True}, {"flash": False, "barcode": False},
                                  {"rate": "25", "df": False, "start": "23:59:58:00"},
                                  {"alpha": True}, {"alpha": True, "colour": True}])
def test_incremental_frames_equal_full_redraws(opts):
    """Only changed cells are redrawn; the result must not depend on that."""
    plan, cfg, layers, info, _ = _setup(**opts)
    inc = pic.Compositor(cfg, layers, info)
    for i in list(range(plan.video_frames)):
        texts, flash, slot = pic.frame_state(plan, cfg, i)
        a = inc.frame(i, texts, flash, slot).copy()
        b = pic.Compositor(cfg, layers, info).frame(i, texts, flash, slot)
        assert np.array_equal(a, b), i


@pytest.mark.parametrize("colour, alpha, shape, fmt", [
    (False, False, (H, W), "gray"), (True, False, (H, W, 3), "rgb24"),
    (False, True, (H, W, 2), "ya8"), (True, True, (H, W, 4), "rgba")])
def test_frames_use_as_few_channels_as_they_need(colour, alpha, shape, fmt):
    plan, cfg, layers, info, _ = _setup(colour=colour, alpha=alpha)
    comp = pic.Compositor(cfg, layers, info)
    f = comp.frame(0, *pic.frame_state(plan, cfg, 0))
    assert f.shape == shape and comp.pix_fmt == fmt and f.flags.c_contiguous


def test_frame_state_follows_the_readout_settings():
    plan, cfg, *_ = _setup()
    texts, flash, slot = pic.frame_state(plan, cfg, 3)
    assert texts["main"] == plan.video_text(3) and texts["sub"] == f"LTC {plan.ltc_text(3)}"
    assert texts["count"] == f"-{plan.remaining_text(3)}"
    assert slot == plan.video_label(3)[3]
    zero = next(i for i in range(1, 200) if plan.video_label(i)[3] == 0)
    assert pic.frame_state(plan, cfg, zero)[1] is True
    assert pic.frame_state(plan, cfg, zero, preview=True)[1] is False
    cfg2 = dict(cfg, main="ltc", secondary=False, countdown=False)
    t2, _, _ = pic.frame_state(plan, cfg2, 3)
    assert t2 == {"main": plan.ltc_text(3), "sub": "", "count": ""}


def test_check_frames_cover_ends_and_a_flash():
    plan, cfg, *_ = _setup()
    frames = pic.check_frames(plan, cfg)
    assert frames[0] == 0 and frames[-1] == plan.video_frames - 1
    assert any(plan.video_label(i)[3] == 0 for i in frames[1:])


def test_layers_round_trip_through_npz(tmp_path):
    plan, cfg, layers, info, _ = _setup()
    path = str(tmp_path / "g.npz")
    pic.save_layers(path, layers, info)
    layers2, info2 = pic.load_layers(path)
    assert np.array_equal(info, info2)
    for i in (0, 77):
        st = pic.frame_state(plan, cfg, i)
        assert np.array_equal(pic.Compositor(cfg, layers, info).frame(i, *st),
                              pic.Compositor(cfg, layers2, info2).frame(i, *st))
