"""LTC File Gen: TouchDesigner extension.

Drives TouchDesigner only: parameters, the picture, stop-frame recording,
the background ffmpeg mux and the read-back check. All timecode, LTC and
ffmpeg logic lives in the plain-Python engine in ../src/ltcfilegen, which is
tested outside TouchDesigner.

Two engines (Video page, Engine):
  TouchDesigner  Movie File Out writes one frame per TouchDesigner frame
                 (NotchLC needs this; the loop follows the display refresh)
  ffmpeg fast    TouchDesigner renders each text line once per digit; a
                 separate process (ltcfilegen.fastrender) assembles every
                 frame with numpy and pipes it into ffmpeg, which also adds
                 the LTC. Several times faster. Auto uses it when it can,
                 after checking its frames against TouchDesigner's, and falls
                 back to TouchDesigner if it fails.

Render loop (one step per cooked frame, from the frame-start Execute DAT):
  TouchDesigner engine:
    arm     record=1, pause=1, let Movie File Out open the file
    record  frame i: set picture, append LTC samples, pulse Add Frame
    close   record=0, wait until Movie File Out reports the file complete
    mux     ffmpeg copies the video and adds the LTC track (background)
  ffmpeg fast engine:
    fast    follow the worker's status file until it exits
  both:
    verify  read the finished file back; fail loudly on any mismatch
"""

import dataclasses
import importlib
import json
import numpy as np
import os
from fractions import Fraction
import re
import subprocess
import sys
import time

# key: (label, videocodec, videocodectype, pixel format, size multiple)
CODECS = {
    'hap':         ('HAP',           'hap',     'hap',         'rgb',         4),
    'hapq':        ('HAP Q',         'hap',     'hapq',        'rgb',         4),
    'notchlc':     ('NotchLC',       'notchlc', None,          'rgb',         4),
    'prores422hq': ('ProRes 422 HQ', 'prores',  'prores422hq', 'yuv42210bit', 2),
    'prores422':   ('ProRes 422',    'prores',  'prores422',   'yuv42210bit', 2),
    'h264':        ('H.264',         'h264',    None,          'yuv420',      2),
    'h265':        ('H.265',         'h265',    None,          'yuv420',      2),
}

RESOLUTIONS = {'3840x2160': (3840, 2160), '2560x1440': (2560, 1440),
               '1920x1080': (1920, 1080), '1280x720': (1280, 720)}

#: Conservative render-speed estimate: one frame per TouchDesigner loop, and
#: the loop follows the display refresh (measured about 118/s on a 120 Hz
#: MacBook Pro display; presumably about 60/s on a 60 Hz display, untested).
EST_FRAMES_PER_SECOND = 60.0

#: Conservative ffmpeg fast-engine speeds at 1920x1080 (frames/s, every
#: picture option on, M4 Max), scaled by pixel count for other sizes.
FAST_EST_FPS = {'hap': 500, 'hapq': 400, 'prores422hq': 400, 'prores422': 400,
                'h264': 400, 'h265': 150}

#: Text TOP of each text line the picture can show (see ltcfilegen.picture).
LINE_TOPS = {'main': 'text_main', 'sub': 'text_sub', 'count': 'text_count'}

FINALIZE_TIMEOUT = 600          # s to wait for Movie File Out after record=0

#: Project FPS while rendering. Measured on TD 2025.33230: when the project
#: runs at a 1001 rate (23.976/29.97/59.94) and the movie FPS is a whole
#: number (24/30/60), Movie File Out writes the PROJECT's rate instead, e.g.
#: a 60 fps file at 60000/1001, or at 30000/1001 in a 29.97 project. In a
#: whole-number project every rate is written exactly. 120 lets the loop run
#: at a 120 Hz display's refresh: renders measured 117 frames/s at 120 and
#: 60/s at 60, with every codec frame-exact (tools/av_check.py). The user's
#: FPS is restored after.
RENDER_COOK_RATE = 120.0
RECORD_CHECK_FRAME = 5          # by then Movie File Out must have opened the file


def _engine_root(ownerComp):
    """Repo folder holding src/ltcfilegen (the extension file lives in td/)."""
    dat = ownerComp.op('LtcGenExt')
    f = dat.par.file.eval() if dat is not None else ''
    if f:
        p = f if os.path.isabs(f) else os.path.join(project.folder, f)
        root = os.path.dirname(os.path.dirname(os.path.abspath(p)))
        if os.path.isdir(os.path.join(root, 'src', 'ltcfilegen')):
            return root
    return project.folder


def _label_for_file(text):
    return text.replace(':', '-').replace(';', '-')


class LtcGenExt:
    """Promoted: Render, Cancel, Refresh, ApplyFrame, CheckFfmpeg, InstallFfmpeg."""

    def __init__(self, ownerComp):
        self.ownerComp = ownerComp
        src = os.path.join(_engine_root(ownerComp), 'src')
        if src not in sys.path:
            sys.path.insert(0, src)
        import ltcfilegen.timecode
        import ltcfilegen.job
        import ltcfilegen.ltc
        import ltcfilegen.ffmpeg
        import ltcfilegen.picture
        # Reload in dependency order so edits on disk take effect on re-init.
        for m in (ltcfilegen.timecode, ltcfilegen.job, ltcfilegen.ltc, ltcfilegen.ffmpeg,
                  ltcfilegen.picture):
            importlib.reload(m)
        self.tc, self.jobs, self.ltc, self.ff = (ltcfilegen.timecode, ltcfilegen.job,
                                                 ltcfilegen.ltc, ltcfilegen.ffmpeg)
        self.pic = ltcfilegen.picture
        self._layouts = {}
        self._anchors = {}
        self.job = None
        self.batch = []
        self.BatchResults = []
        self.cleanup = []
        self.install = None
        self.plan = None
        self.cfg = None
        self.ffinfo = None
        self.ffprobe = None
        self._ff_checked = False
        interrupted = self._recover_interrupted()
        self._set('Rendering', False)
        self._set('Batchcount', len(self._rows()))
        self._write_batch_view()
        self.Refresh()
        # Check ffmpeg a few frames after start-up (it runs ffmpeg up to three
        # times), so the help button is right on every machine, not only
        # after the first render.
        run('args[0].ext.LtcGenExt.CheckFfmpeg(force=True)', self.ownerComp,
            delayFrames=15, fromOP=self.ownerComp)
        if interrupted:
            self._status('The last render was interrupted (the extension reloaded); '
                         'its temporary files are being removed. Render again.')

    def _recover_interrupted(self):
        """A re-init (file sync, reload) wipes self.job. If a render was active,
        stop Movie File Out and any ffmpeg mux, and remove its temporary files."""
        stale = self.ownerComp.fetch('active_job', None, search=False)
        restore = self.ownerComp.fetch('batch_restore', None, search=False)
        if restore:
            self._batch_restore = restore
            self._restore_batch_settings()
        if not stale:
            return False
        mvo = self.ownerComp.op('mvo')
        if mvo is not None:
            mvo.par.record = 0
            mvo.par.pause = 0
        if stale.get('orig_cook'):
            project.cookRate = stale['orig_cook']
        # The ffmpeg mux, or the fast worker and its ffmpeg. A pid may have
        # been reused since, so only stop processes that are still ours.
        for pid in [stale.get('pid')] + list(stale.get('pids') or []):
            if pid and self._is_our_process(pid):
                try:
                    os.kill(pid, 15)
                except OSError:
                    pass
        stem = stale['stem']
        self.cleanup = [f for f in [stale.get('video'), stale.get('pcm'), stem + '.muxing.mov',
                                    stem + '.mux.progress', stem + '.mux.log']
                        + list(stale.get('temp') or []) if f]
        self._cleanup_t0 = time.time()
        self.ownerComp.unstore('active_job')
        return True

    # ------------------------------------------------------------------
    # Operators and parameters

    @property
    def pars(self):
        return self.ownerComp.par

    def child(self, name):
        o = self.ownerComp.op(name)
        if o is None:
            raise RuntimeError(f'{self.ownerComp.path}/{name} is missing')
        return o

    def _set(self, name, value):
        par = getattr(self.pars, name, None)
        if par is not None and par.eval() != value:
            par.val = value

    def _status(self, text, log=True):
        self._set('Status', text)
        if log:
            debug(f'LTC File Gen: {text}')

    # ------------------------------------------------------------------
    # Settings -> validated job

    def _settings(self):
        """Read every parameter into (plan, audio options, cfg). Raises ValueError."""
        p = self.pars
        rate = p.Rate.eval()
        spec = self.jobs.JobSpec(rate=rate,
                                 drop_frame=bool(p.Dropframe.eval()) and rate in ('29.97', '59.94'),
                                 start=p.Start.eval(), duration=p.Duration.eval(),
                                 preroll=p.Preroll.eval())
        plan = self.jobs.Plan(spec)

        codec = p.Codec.eval()
        if codec not in CODECS:
            raise ValueError(f'unknown codec {codec!r}')
        res = p.Resolution.eval()
        w, h = RESOLUTIONS[res] if res in RESOLUTIONS else (int(p.Customw.eval()), int(p.Customh.eval()))
        mult = CODECS[codec][4]
        if not (16 <= w <= 8192 and 16 <= h <= 8192):
            raise ValueError(f'resolution {w}x{h} is out of range (16-8192)')
        if w % mult or h % mult:
            raise ValueError(f'{CODECS[codec][0]} needs width and height divisible by {mult} '
                             f'({w}x{h} is not)')

        ub_text = (p.Userbits.eval() or '0').strip()
        try:
            ub = int(ub_text, 16)
        except ValueError:
            raise ValueError(f'user bits {ub_text!r} must be up to 8 hex digits') from None
        rise = None if p.Risemode.eval() == 'standard' else float(p.Risetime.eval())
        audio = self.ltc.AudioOptions(sample_rate=int(p.Samplerate.eval()),
                                      bit_depth=int(p.Bitdepth.eval()),
                                      layout=p.Layout.eval(), level_dbfs=float(p.Level.eval()),
                                      rise_us=rise, user_bits=ub)
        audio.validate()

        cfg = {
            'w': w, 'h': h, 'codec': codec,
            'main': p.Mainreadout.eval(), 'secondary': bool(p.Secondary.eval()),
            'countdown': bool(p.Countdown.eval()), 'flash': bool(p.Syncflash.eval()),
            'marker': bool(p.Framemarker.eval()), 'info': bool(p.Infocaption.eval()),
            'barcode': bool(p.Debugbarcode.eval()),
            'font': p.Font.eval() or 'Arial', 'scale': float(p.Textsize.eval()),
            'fg': (float(p.Textcolorr), float(p.Textcolorg), float(p.Textcolorb)),
            'bg': (float(p.Bgcolorr), float(p.Bgcolorg), float(p.Bgcolorb)),
            'tctrack': bool(p.Timecodetrack.eval()),
        }
        cfg['strip_h'] = self.barcode_strip_height(h)
        cfg['marker_h'] = max(8, int(round(h / 40.0)) // 2 * 2)
        cfg['info_text'] = self._info_text(plan, audio, cfg)
        return plan, audio, cfg

    @staticmethod
    def barcode_strip_height(h):
        """Height of the debug barcode strip (tools/av_check.py reads it)."""
        return max(16, int(round(h / 30.0)) // 2 * 2)

    def _info_text(self, plan, audio, cfg):
        df = ' DF' if plan.drop_frame else (' NDF' if plan.rate.drop_allowed else '')
        parts = [f'{plan.rate.key}{df}', f"{cfg['w']}x{cfg['h']}", CODECS[cfg['codec']][0],
                 f"start {plan.summary()['start']}", f'duration {self.pars.Duration.eval()}']
        if plan.preroll_frames:
            parts.append(f'pre-roll {self.pars.Preroll.eval()}')
        parts.append(f'LTC {audio.level_dbfs:g} dBFS')
        return '   '.join(parts)

    def Refresh(self, update_status=True):
        """Re-read the parameters, update the summary and the preview."""
        if self.job:
            return
        try:
            plan, audio, cfg = self._settings()
        except (ValueError, self.tc.TimecodeError) as exc:
            self.plan = None
            self._set('Summary', '')
            self._status(f'Check settings: {exc}')
            return
        self.plan, self.audio, self.cfg = plan, audio, cfg
        secs = plan.video_frames / float(plan.rate.fps)
        pref = self.pars.Engine.eval() if hasattr(self.pars, 'Engine') else 'auto'
        if pref != 'td' and cfg['codec'] in FAST_EST_FPS and self.ffinfo is not None:
            how = 'ffmpeg fast'
            est = plan.video_frames / (FAST_EST_FPS[cfg['codec']] * 1920 * 1080 / (cfg['w'] * cfg['h']))
        else:
            how = 'TouchDesigner'
            est = plan.video_frames / EST_FRAMES_PER_SECOND
        when = 'under a minute' if est < 60 else f'about {round(est / 60)} min'
        s = plan.summary()
        self._set('Summary', f"{s['first']} to {s['end']}  |  {plan.video_frames:,} frames  |  "
                             f"{int(secs // 3600)}:{int(secs % 3600 // 60):02d}:{secs % 60:05.2f}  |  "
                             f"render {when} ({how})")
        self.ApplySettings(cfg)
        self.ApplyFrame(0, plan, cfg, preview=True)
        if update_status:
            self._status('Ready')

    # ------------------------------------------------------------------
    # Picture

    def ApplySettings(self, cfg):
        """Resolution, fonts, sizes and switches for the whole picture."""
        w, h, sc = cfg['w'], cfg['h'], cfg['scale']
        texts = {
            'text_main':  (0.16 * h * sc, 'center', 'center', 0.0, 0.0),
            'text_sub':   (0.05 * h * sc, 'center', 'center', 0.0, -0.12 * h * sc),
            'text_count': (0.05 * h * sc, 'center', 'center', 0.0, 0.12 * h * sc),
            'text_info':  (0.022 * h, 'left', 'bottom', 0.02 * w, cfg['marker_h'] + 0.015 * h),
        }
        for name, (size, ax, ay, px, py) in texts.items():
            t = self.child(name)
            t.par.outputresolution = 'custom'
            t.par.resolutionw, t.par.resolutionh = w, h
            t.par.font = cfg['font']
            t.par.fontsizexunit = 'pixels'
            t.par.fontsizex = size
            t.par.alignx, t.par.aligny = ax, ay
            t.par.positionunit = 'pixels'
            t.par.positionx, t.par.positiony = px, py
            t.par.bgalpha = 0
        self.child('text_info').par.text = cfg['info_text'] if cfg['info'] else ''
        # Readout lines are drawn from fixed character positions (Specification
        # DAT), so digits never shift sideways; see ltcfilegen.picture.LineLayout.
        self._layouts = {}
        self._anchors = {}
        for line, top in LINE_TOPS.items():
            t = self.child(top)
            t.par.text = ''
            t.par.specdat = 'spec_' + line
            _, _, _, px, py = texts[top]
            self._anchors[line] = (w / 2.0 + px, h / 2.0 + py)
        g = self.child('glsl_picture')
        g.par.outputresolution = 'custom'
        g.par.resolutionw, g.par.resolutionh = w, h
        g.par.vec2valuex = 1.0 if cfg['barcode'] else 0.0
        g.par.vec2valuez = cfg['strip_h']
        g.par.vec3valuex = 1.0 if cfg['marker'] else 0.0
        g.par.vec3valuew = cfg['marker_h']
        g.par.vec4valuex = 0.7

    def ApplyFrame(self, i, plan=None, cfg=None, preview=False):
        """Set everything that changes per frame for video frame i.
        preview=True never flashes, so the idle preview stays readable."""
        plan = plan or self.plan
        cfg = cfg or self.cfg
        if plan is None or cfg is None:
            return
        texts, flash, slot = self.pic.frame_state(plan, cfg, i, preview=preview)
        for line, text in texts.items():
            self._set_line(line, text)
        bg, fg = (cfg['fg'], cfg['bg']) if flash else (cfg['bg'], cfg['fg'])
        g = self.child('glsl_picture')
        g.par.vec0valuex, g.par.vec0valuey, g.par.vec0valuez = bg
        g.par.vec1valuex, g.par.vec1valuey, g.par.vec1valuez = fg
        g.par.vec2valuey = i
        g.par.vec3valuey = slot
        g.par.vec3valuez = plan.rate.nominal

    def _line_layout(self, line, tmpl):
        """Fixed character positions for a line, from its Text TOP's font."""
        t = self.child(LINE_TOPS[line])
        cx, cy = self._anchors.get(line, (t.width / 2.0, t.height / 2.0))
        # Same vertical place as the Text TOP's own centring (metrics mode),
        # measured on 2025.33230: baseline = centre - height/2 + descender/2.
        height = t.evalTextSize('0')[1]
        baseline = cy - height / 2.0 + t.fontDescender / 2.0
        return self.pic.LineLayout(tmpl, lambda s: t.evalTextSize(s)[0], cx, baseline)

    def _set_line(self, line, text):
        """Show text on a line, rewriting only the table cells that change."""
        spec = self.child('spec_' + line)
        tmpl = self.pic.template(text)
        layout, old = self._layouts.get(line, (None, None))
        if old is None or (layout.template if layout else '') != tmpl:
            layout = self._line_layout(line, tmpl) if text else None
            rows = layout.rows(text) if layout else []
            spec.clear()
            spec.appendRow(['x', 'y', 'text'])
            for r in rows:
                spec.appendRow(list(r))
            self._layouts[line] = (layout, rows)
            return
        if layout is None:
            return
        rows = layout.rows(text)
        for k, (new, prev) in enumerate(zip(rows, old)):
            if new != prev:
                spec[k + 1, 0] = new[0]
                spec[k + 1, 2] = new[2]
        self._layouts[line] = (layout, rows)

    # ------------------------------------------------------------------
    # Fast engine, picture side: TouchDesigner renders each text line once
    # per digit; numpy assembles the frames (ltcfilegen.picture).

    def _alpha(self, top, cfg):
        """A Text TOP's alpha as uint8, top row first."""
        t = self.child(top)
        t.cook(force=True)
        a = t.numpyArray(delayed=False)
        if a is None or a.shape[:2] != (cfg['h'], cfg['w']):
            got = 'nothing' if a is None else f'{a.shape[1]}x{a.shape[0]}'
            raise self.pic.PictureError(f'TouchDesigner drew the text at {got}, not '
                                        f"{cfg['w']}x{cfg['h']} (licence size limit?)")
        return np.ascontiguousarray(np.flipud(np.floor(a[:, :, 3] * 255 + 0.5)).astype(np.uint8))

    def _picture(self, cfg):
        """The finished picture (null_out) as uint8 RGB, top row first."""
        n = self.child('null_out')
        n.cook(force=True)
        a = n.numpyArray(delayed=False)
        return np.ascontiguousarray(np.flipud(np.floor(a[:, :, :3] * 255 + 0.5)).astype(np.uint8))

    def FastGlyphs(self, plan, cfg):
        """Glyph cells for the fast engine, checked against TouchDesigner.

        Renders each line with every digit 0..9, then compares assembled lines
        (mixed digits) and whole frames with TouchDesigner's own pictures.
        Returns (layers, info). Raises PictureError on any difference."""
        pic = self.pic
        cfg = dict(cfg, slots=plan.rate.nominal)
        self.ApplySettings(cfg)
        texts, _, _ = pic.frame_state(plan, cfg, 0)
        layers = {}
        for line, top in LINE_TOPS.items():
            layers[line] = None
            if not texts[line]:
                self._set_line(line, '')
                continue
            tmpl = pic.template(texts[line])
            refs = []
            for d in range(10):
                self._set_line(line, pic.with_digit(tmpl, d))
                refs.append(self._alpha(top, cfg))
            layer = pic.TextLayer.from_refs(tmpl, refs)
            if layer is not None:
                for s in pic.check_strings(tmpl):
                    self._set_line(line, s)
                    if not np.array_equal(self._alpha(top, cfg), layer.render(s, cfg['h'], cfg['w'])):
                        raise pic.PictureError(f'the {line} line ({s}) does not assemble exactly '
                                               f'from single digits in this font')
            layers[line] = layer
        info = self._alpha('text_info', cfg) if cfg['info'] else None
        for i in pic.check_frames(plan, cfg):
            self.ApplyFrame(i, plan, cfg)
            want = self._picture(cfg)
            texts, flash, slot = pic.frame_state(plan, cfg, i)
            got = pic.Compositor(cfg, layers, info).frame(i, texts, flash, slot)
            if got.ndim == 2:
                got = np.repeat(got[:, :, None], 3, axis=2)
            diff = int(np.abs(want.astype(np.int16) - got).max())
            if diff > 1:
                raise pic.PictureError(f'frame {i} differs from TouchDesigner by up to {diff}/255')
        return layers, info

    # ------------------------------------------------------------------
    # ffmpeg

    def CheckFfmpeg(self, force=True):
        """Find a usable ffmpeg per the Setup page. Returns FfmpegInfo or None."""
        if self._ff_checked and not force:
            return self.ffinfo
        p = self.pars
        info, tried = self.ff.find_ffmpeg(override=(p.Ffmpegpath.eval() or None),
                                          source=p.Ffmpegsource.eval(),
                                          share=bool(p.Shareffmpeg.eval()))
        self.ffinfo, self._ff_checked = info, True
        self.ffprobe = self.ff.find_ffprobe(info.path) if info else None
        if info:
            self._set('Ffmpegstatus', f'Ready: ffmpeg {info.version} ({info.source})')
            self._set('Ffmpegready', True)
        else:
            why = '; '.join(f'{t.path or t.source}: {t.reason}' for t in tried[:3]) or 'none found'
            self._set('Ffmpegstatus', f'Not found ({why}). Pulse Install ffmpeg.')
            self._set('Ffmpegready', False)
        if not self.job and self.plan is not None:
            self.Refresh(update_status=False)     # the estimate depends on the engine
        return info

    def InstallFfmpeg(self):
        """Download imageio-ffmpeg into the user data folder, in the background."""
        if self.install:
            return
        py = self.ff.python_for_pip()
        if py is None:
            self._set('Ffmpegstatus', 'No Python with pip found; install ffmpeg manually '
                                      '(e.g. brew install ffmpeg) and pulse Check ffmpeg.')
            return
        self.install = {'proc': self.ff.start_install(py), 't0': time.time()}
        self._set('Ffmpegstatus', 'Installing ffmpeg (about 30 MB)...')

    def _tick_install(self):
        rc = self.install['proc'].poll()
        if rc is None:
            secs = int(time.time() - self.install['t0'])
            self._set('Ffmpegstatus', f'Installing ffmpeg... {secs}s')
            return
        self.install = None
        if rc == 0 and self.CheckFfmpeg():
            return
        log = os.path.join(self.ff.pylibs_dir(), 'install.log')
        self._set('Ffmpegstatus', f'Install failed (pip exit {rc}); see {log}')

    # ------------------------------------------------------------------
    # Render

    def _filename(self, plan, cfg):
        s = plan.summary()
        df = 'DF' if plan.drop_frame else ('NDF' if plan.rate.drop_allowed else '')
        tokens = {
            'start': _label_for_file(s['start']), 'first': _label_for_file(s['first']),
            'end': _label_for_file(s['end']), 'dur': _label_for_file(self.pars.Duration.eval()),
            'preroll': _label_for_file(self.pars.Preroll.eval()),
            'rate': plan.rate.key, 'tc': plan.rate.key + df,
            'res': f"{cfg['w']}x{cfg['h']}", 'codec': CODECS[cfg['codec']][0].replace(' ', '').replace('.', ''),
        }
        pattern = self.pars.Filename.eval() or 'LTC_{start}_{dur}_{tc}_{res}_{codec}'
        try:
            name = pattern.format(**tokens)
        except (KeyError, IndexError, ValueError) as exc:
            raise ValueError(f'file name pattern: unknown token {exc}') from None
        name = re.sub(r'[\\/:*?"<>|]+', '-', name).strip() or 'LTC'
        return name[:-4] if name.lower().endswith('.mov') else name

    def Render(self):
        if self.job:
            self._status('Already rendering')
            return
        try:
            plan, audio, cfg = self._settings()
        except (ValueError, self.tc.TimecodeError) as exc:
            self._status(f'Cannot render: {exc}')
            return
        folder = self.pars.Outputfolder.eval()
        if not folder or not os.path.isdir(folder):
            self._status('Cannot render: choose an existing output folder')
            return
        try:
            stem = os.path.join(folder, self._filename(plan, cfg))
        except ValueError as exc:
            self._status(f'Cannot render: {exc}')
            return
        final = stem + '.mov'
        if os.path.exists(final) and not self.pars.Overwrite.eval():
            n = 2
            while os.path.exists(f'{stem}_{n}.mov'):
                n += 1
            stem = f'{stem}_{n}'
            final = stem + '.mov'

        ffinfo = self.CheckFfmpeg(force=True)
        in_batch = getattr(self, '_batch_restore', None) is not None
        if ffinfo is None and not self.install and in_batch:
            self._status('Cannot render: ffmpeg not found (click ? for Setup & Help)')
            return
        if ffinfo is None and not self.install:
            choice = ui.messageBox('LTC File Gen',
                                   'ffmpeg is needed to add the LTC audio and was not found.\n\n'
                                   'Install it now? The video renders while it downloads.',
                                   buttons=['Install and render', 'Cancel'])
            if choice != 0:
                self._status('Cancelled: ffmpeg is not installed')
                return
            self.InstallFfmpeg()

        pref = self.pars.Engine.eval() if hasattr(self.pars, 'Engine') else 'auto'
        engine, why = self._pick_engine(pref, cfg)
        if engine == 'fast':
            try:
                self._start_fast(plan, audio, cfg, stem, final, auto=(pref == 'auto'))
                return
            except Exception as exc:
                why = f'{exc}'
                self.job = None
                self.ApplyFrame(0, plan, cfg, preview=True)
        if pref == 'ffmpeg':
            self._status(f'Cannot render with ffmpeg fast: {why}')
            return
        self._start_td(plan, audio, cfg, stem, final, note=why if pref == 'auto' else None)

    def _pick_engine(self, pref, cfg):
        """('fast', None) when the ffmpeg fast engine can make this codec,
        else ('td', reason). Probes are cached, so this is quick after the
        first time."""
        if pref == 'td':
            return 'td', None
        label = CODECS[cfg['codec']][0]
        if cfg['codec'] not in self.ff.FAST_ENCODERS:
            return 'td', f'{label} has no ffmpeg encoder'
        if self.ffinfo is None:
            return 'td', 'ffmpeg is not installed yet'
        enc = self.ff.fast_encoder(self.ffinfo.path, cfg['codec'])
        if enc is None:
            return 'td', f'this ffmpeg ({self.ffinfo.source or self.ffinfo.path}) cannot encode {label}'
        py = self.ff.python_for_worker()
        if py is None:
            return 'td', 'no Python with numpy was found to run it'
        self._fast_tools = (enc, py)
        return 'fast', None

    def _start_fast(self, plan, audio, cfg, stem, final, auto):
        """Start the ffmpeg fast engine: glyphs from TouchDesigner, frames and
        encode in a separate process (ltcfilegen.fastrender)."""
        enc, py = self._fast_tools
        layers, info = self.FastGlyphs(plan, cfg)
        self.ApplyFrame(0, plan, cfg, preview=True)
        paths = {k: stem + suffix for k, suffix in (
            ('glyphs', '.glyphs.npz'), ('job', '.fast.json'), ('status', '.fast.status'),
            ('log', '.fast.log'), ('out_log', '.fast.out'), ('pcm', '.ltc.pcm'), ('out', '.muxing.mov'))}
        self._claim(list(paths.values()) + [paths['status'] + '.tmp'])
        for f in paths.values():
            if os.path.exists(f):
                os.remove(f)
        self.pic.save_layers(paths['glyphs'], layers, info)
        keys = ('w', 'h', 'fg', 'bg', 'main', 'secondary', 'countdown', 'flash', 'marker',
                'marker_h', 'barcode', 'strip_h')
        job = {'spec': dataclasses.asdict(plan.spec), 'audio': dataclasses.asdict(audio),
               'cfg': {k: cfg[k] for k in keys}, 'glyphs': paths['glyphs'], 'pcm': paths['pcm'],
               'out': paths['out'], 'status': paths['status'], 'log': paths['log'],
               'ffmpeg': self.ffinfo.path, 'encoder': dataclasses.asdict(enc),
               'timecode': plan.embedded_timecode if cfg['tctrack'] else None}
        with open(paths['job'], 'w') as fh:
            json.dump(job, fh)
        src = os.path.join(_engine_root(self.ownerComp), 'src')
        env = dict(os.environ)
        env['PYTHONPATH'] = src + (os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
        kw = {'creationflags': 0x08000000} if os.name == 'nt' else {}
        out_log = open(paths['out_log'], 'wb')
        proc = subprocess.Popen([py, '-m', 'ltcfilegen.fastrender', paths['job']], stdout=out_log,
                                stderr=out_log, stdin=subprocess.DEVNULL, env=env, cwd=src, **kw)
        temp = [paths[k] for k in ('glyphs', 'job', 'status', 'log', 'out_log')] + [paths['status'] + '.tmp']
        self.job = {'state': 'fast', 'engine': 'ffmpeg fast', 'encoder': enc.name, 'i': 0,
                    'plan': plan, 'cfg': cfg, 'enc': self.ltc.LtcEncoder(plan, audio), 'audio': audio,
                    'pcm_path': paths['pcm'], 'video': None, 'final': final, 'stem': stem,
                    't0': time.time(), 'proc': proc, 'log': out_log, 'log_path': paths['out_log'],
                    'mux_out': paths['out'], 'fast_status': paths['status'], 'temp': temp,
                    'pids': [proc.pid], 'auto': auto}
        self._store_active(self.job)
        self._set('Progress', 0.0)
        self._set('Rendering', True)
        self._status(f'Rendering {os.path.basename(final)} (ffmpeg fast, {enc.name})')

    def _claim(self, paths):
        """A new job owns these files: take them off the queued cleanup of an
        earlier job with the same name, which would otherwise delete them."""
        self.cleanup = [f for f in self.cleanup if f not in set(paths)]

    def _store_active(self, j):
        """Persist what a re-init needs to stop processes and remove files."""
        self.ownerComp.store('active_job', {
            'video': j.get('video'), 'pcm': j['pcm_path'], 'stem': j['stem'],
            'pid': j['proc'].pid if j.get('proc') else None, 'pids': list(j.get('pids', [])),
            'temp': list(j.get('temp', [])), 'orig_cook': j.get('orig_cook')})

    def _start_td(self, plan, audio, cfg, stem, final, note=None):
        """Render through Movie File Out, one frame per TouchDesigner frame."""
        self._claim([stem + x for x in ('.video.partial.mov', '.ltc.pcm', '.muxing.mov',
                                         '.mux.progress', '.mux.log')])
        enc = self.ltc.LtcEncoder(plan, audio)
        video = stem + '.video.partial.mov'
        pcm_path = stem + '.ltc.pcm'
        for f in (video, pcm_path):
            if os.path.exists(f):
                os.remove(f)
        mvo = self.child('mvo')
        label, vcodec, vtype, pix, _ = CODECS[cfg['codec']]
        mvo.par.record = 0
        mvo.par.type = 'stopframemovie'
        mvo.par.videocodec = vcodec
        if vtype:
            mvo.par.videocodectype = vtype
        mvo.par.videopixelformat = pix
        mvo.par.fps = plan.rate.td_fps
        mvo.par.audiochop = ''
        mvo.par.file = video
        self.ApplySettings(cfg)
        self.job = {'state': 'arm', 'ticks': 0, 'i': 0, 'plan': plan, 'cfg': cfg, 'enc': enc,
                    'audio': audio, 'pcm': open(pcm_path, 'wb'), 'pcm_path': pcm_path,
                    'video': video, 'final': final, 'stem': stem, 't0': time.time(),
                    'proc': None, 'log': None, 'engine': 'TouchDesigner'}
        self._set('Progress', 0.0)
        self.job['orig_cook'] = project.cookRate
        self._store_active(self.job)
        project.cookRate = RENDER_COOK_RATE
        self._set('Rendering', True)
        mvo.par.record = 1
        mvo.par.pause = 1
        self._status(f'Rendering {os.path.basename(final)}' + (f' in TouchDesigner ({note})' if note else ''))

    def _busy(self):
        """A job is running, or a batch is between jobs."""
        return bool(self.job) or getattr(self, '_batch_restore', None) is not None

    def Cancel(self):
        for overrides in self.batch:
            self._set_row_status(overrides, 'cancelled')
        self.batch = []
        if not self.job:
            self._restore_batch_settings()
            return
        self._end_job('Cancelled', keep=False)

    # ------------------------------------------------------------------
    # Batch: a list of parameter overrides rendered one after another

    def RenderBatch(self, jobs):
        """Render each dict of parameter values in turn, e.g.
        [{'Duration': '01:00:00:00'}, {'Duration': '02:00:00:00', 'Start': '02:00:00:00'}].
        Results land in BatchResults as (overrides, status, file or None)."""
        if self._busy():
            self._status('Busy: a render or batch is running')
            return
        self.batch = [dict(j) for j in jobs]
        self.BatchResults = []
        keys = {k for j in self.batch for k in j if not k.startswith('_')}
        self._batch_restore = {k: getattr(self.pars, k).eval() for k in keys if hasattr(self.pars, k)}
        self.ownerComp.store('batch_restore', self._batch_restore)
        self._batch_total = len(self.batch)
        self._next_batch()

    def _next_batch(self):
        while getattr(self, 'batch', None):
            overrides = self.batch.pop(0)
            self._batch_current = overrides
            for k, v in overrides.items():
                if not k.startswith('_'):
                    setattr(self.pars, k, v)
            self._set_row_status(overrides, 'rendering')
            self.Render()
            if self.job:
                self.job['batch'] = overrides
                return
            self._set_row_status(overrides, self.pars.Status.eval())
            self.BatchResults.append((overrides, self.pars.Status.eval(), None))
        # Every remaining item failed validation: finish the batch here.
        self._set('Rendering', False)
        self._restore_batch_settings()
        n_ok = sum(1 for r in self.BatchResults if r[2])
        self._status(f'Batch finished: {n_ok}/{len(self.BatchResults)} rendered.')
        self.Refresh(update_status=False)

    def OnFrame(self):
        """Called at the start of every frame by the Execute DAT."""
        if self.install:
            self._tick_install()
        if self.cleanup and (not self._mvo_writing() or time.time() - self._cleanup_t0 > FINALIZE_TIMEOUT):
            for f in self.cleanup:
                try:
                    if os.path.exists(f):
                        os.remove(f)
                except OSError as exc:
                    debug(f'LTC File Gen: could not delete {f}: {exc}')
            self.cleanup = []
        j = self.job
        if not j:
            return
        try:
            getattr(self, '_tick_' + j['state'])(j)
        except Exception as exc:              # never leave Movie File Out recording
            self._end_job(f'Failed: {type(exc).__name__}: {exc}', keep=False)

    def _tick_fast(self, j):
        """Follow the fast worker; on success go on to the read-back check."""
        try:
            with open(j['fast_status']) as fh:
                st = json.load(fh)
        except (OSError, ValueError):
            st = {}
        ffpid = st.get('ffmpeg_pid')
        if ffpid and ffpid not in j['pids']:
            j['pids'].append(ffpid)
            self._store_active(j)
        rc = j['proc'].poll()
        if rc is None:
            phase = st.get('phase')
            if phase == 'audio':
                self._set('Progress', round(0.05 * st.get('fraction', 0.0), 4))
                self._status('Writing the LTC audio...', log=False)
            elif phase == 'video' and st.get('total'):
                fps = st.get('fps') or 0.0
                left = (st['total'] - st['frame']) / fps if fps else 0.0
                self._set('Progress', round(0.05 + 0.9 * st.get('fraction', 0.0), 4))
                self._status(f'Rendering {st["frame"]:,}/{st["total"]:,}  ({fps:.0f} fps, '
                             f'{int(left // 60)}:{int(left % 60):02d} left, ffmpeg fast)', log=False)
            elif phase == 'finishing':
                self._set('Progress', 0.95)
                self._status('Finishing the file...', log=False)
            return
        j['log'].close()
        if rc == 0 and st.get('phase') == 'done':
            j['state'] = 'verify'
            return
        err = st.get('error') or self._tail(j['log_path']) or f'exit code {rc}'
        if not j.get('auto'):
            raise RuntimeError(f'ffmpeg fast render failed: {err}')
        # Automatic engine: never fail where TouchDesigner would succeed.
        debug(f'LTC File Gen: ffmpeg fast render failed, using TouchDesigner: {err}')
        self._kill_pids(j)
        # The TouchDesigner render reuses the .ltc.pcm and .muxing.mov names,
        # so remove the fast render's files now rather than queue them.
        for f in j['temp'] + [j['mux_out'], j['pcm_path']]:
            try:
                if f and os.path.exists(f):
                    os.remove(f)
            except OSError:
                self.cleanup.append(f)
        batch = j.get('batch')
        self.job = None
        self._start_td(j['plan'], j['audio'], j['cfg'], j['stem'], j['final'],
                       note='ffmpeg fast render failed')
        if batch is not None and self.job:
            self.job['batch'] = batch

    @staticmethod
    def _tail(path, n=400):
        try:
            with open(path, 'rb') as fh:
                fh.seek(max(0, os.path.getsize(path) - n))
                return fh.read().decode('utf-8', 'replace').strip()
        except OSError:
            return ''

    @staticmethod
    def _is_our_process(pid):
        """True if pid is a running ffmpeg or Python (the mux, the fast worker
        or its ffmpeg). A pid of an exited process may belong to anything."""
        try:
            if os.name == 'nt':
                name = subprocess.run(['tasklist', '/FI', f'PID eq {pid}', '/FO', 'CSV', '/NH'],
                                      capture_output=True, text=True, timeout=5,
                                      creationflags=0x08000000).stdout
            else:
                name = subprocess.run(['ps', '-p', str(pid), '-o', 'comm='], capture_output=True,
                                      text=True, timeout=5).stdout
        except Exception:
            return False
        name = name.lower()
        return 'ffmpeg' in name or 'python' in name

    def _kill_pids(self, j):
        """Stop the fast worker and its ffmpeg (ffmpeg would otherwise finish
        a truncated file when its input closes)."""
        for pid in reversed(j.get('pids', [])):
            if self._is_our_process(pid):
                try:
                    os.kill(pid, 9 if os.name != 'nt' else 15)
                except (OSError, ValueError):
                    pass

    def _tick_arm(self, j):
        j['ticks'] += 1
        if j['ticks'] >= 2:
            j['state'] = 'record'
            j['t_rec'] = time.time()

    def _tick_record(self, j):
        plan, enc, i = j['plan'], j['enc'], j['i']
        if i >= plan.video_frames:            # last frame went out last tick
            self.child('mvo').par.record = 0
            j['pcm'].close()
            j['state'], j['t_close'] = 'close', time.time()
            self._status('Finalizing video...')
            return
        self.ApplyFrame(i, plan, j['cfg'])
        a, b = plan.sample_range(i, enc.sr)
        j['pcm'].write(enc.to_pcm(enc.render(a, b)))
        self.child('mvo').par.addframe.pulse()
        j['i'] = i + 1
        if j['i'] == RECORD_CHECK_FRAME and not self._mvo_recording():
            raise RuntimeError(f'Movie File Out did not start recording {CODECS[j["cfg"]["codec"]][0]}: '
                               f'{self._mvo_errors() or "no error given"}. This codec may not be '
                               f'available in this TouchDesigner build or licence.')
        if j['i'] % 15 == 0 or j['i'] == plan.video_frames:
            frac = j['i'] / plan.video_frames
            rate = j['i'] / max(1e-6, time.time() - j['t_rec'])
            left = (plan.video_frames - j['i']) / max(1e-6, rate)
            self._set('Progress', round(0.9 * frac, 4))
            self._status(f'Rendering {j["i"]:,}/{plan.video_frames:,}  '
                         f'({rate:.0f} fps, {int(left // 60)}:{int(left % 60):02d} left)', log=False)

    def _tick_close(self, j):
        waited = time.time() - j['t_close']
        if self._mvo_writing() or not os.path.isfile(j['video']):
            if waited > FINALIZE_TIMEOUT:
                raise RuntimeError('Movie File Out did not finish the video file')
            return
        if self.ffinfo is None:
            if self.install:
                self._status('Video done; waiting for the ffmpeg install...')
                return
            if self.CheckFfmpeg() is None:
                self._end_job(f'Failed: ffmpeg not available. Video and LTC kept: '
                              f'{j["video"]}, {j["pcm_path"]}', keep=True)
                return
        plan, audio = j['plan'], j['audio']
        pcm = {'path': j['pcm_path'], 'sample_rate': audio.sample_rate,
               'channels': audio.channels, 'format': audio.ffmpeg_format}
        j['mux_out'] = j['stem'] + '.muxing.mov'
        j['progress'] = j['stem'] + '.mux.progress'
        # Safety net: if Movie File Out still labelled the file 1000/1001 off
        # (see RENDER_COOK_RATE), the frames are right; rescale the timestamps
        # during the copy mux. The read-back check still has the final say.
        written = self.ff.video_rate(self.ffinfo.path, self.ffprobe, j['video'])
        retime = None
        if written is not None and written != plan.rate.fps:
            retime = written / plan.rate.fps
            if retime not in (Fraction(1000, 1001), Fraction(1001, 1000)):
                raise RuntimeError(f'TouchDesigner wrote {written} fps, expected {plan.rate.fps}')
            j['retimed'] = f'{written.numerator}/{written.denominator}'
        cmd = self.ff.mux_command(self.ffinfo.path, j['video'], pcm, j['mux_out'],
                                  timecode=plan.embedded_timecode if j['cfg']['tctrack'] else None,
                                  progress_path=j['progress'], retime=retime)
        j['log_path'] = j['stem'] + '.mux.log'
        j['log'] = open(j['log_path'], 'wb')
        kw = {'creationflags': 0x08000000} if os.name == 'nt' else {}
        j['proc'] = subprocess.Popen(cmd, stdout=j['log'], stderr=j['log'],
                                     stdin=subprocess.DEVNULL, **kw)
        self.ownerComp.store('active_job', {'video': j['video'], 'pcm': j['pcm_path'],
                                            'stem': j['stem'], 'pid': j['proc'].pid,
                                            'orig_cook': j.get('orig_cook')})
        j['state'], j['t_mux'] = 'mux', time.time()
        self._status('Adding LTC audio...')

    def _tick_mux(self, j):
        rc = j['proc'].poll()
        if rc is None:
            done = self._mux_progress(j)
            if done is not None:
                self._set('Progress', round(0.9 + 0.09 * done, 4))
            return
        j['log'].close()
        if rc != 0:
            tail = open(j['log_path'], errors='replace').read()[-400:]
            raise RuntimeError(f'ffmpeg mux failed (exit {rc}): {tail.strip()}')
        j['state'] = 'verify'

    def _mux_progress(self, j):
        try:
            with open(j['progress'], 'rb') as fh:
                fh.seek(max(0, os.path.getsize(j['progress']) - 600))
                tail = fh.read().decode('utf-8', 'replace')
            us = [int(x) for x in re.findall(r'out_time_us=(\d+)', tail)]
            total = j['plan'].video_frames / float(j['plan'].rate.fps)
            return min(1.0, us[-1] / 1e6 / total) if us and total else None
        except (OSError, ValueError):
            return None

    def _tick_verify(self, j):
        plan, enc, cfg = j['plan'], j['enc'], j['cfg']
        got = self.ff.probe_movie(self.ffinfo.path, self.ffprobe, j['mux_out'])
        problems = []
        if 'frames' in got and got['frames'] != plan.video_frames:
            problems.append(f"{got['frames']} video frames, expected {plan.video_frames}")
        if 'rate' in got and got['rate'] != plan.rate.fps:
            problems.append(f"frame rate {got['rate']}, expected {plan.rate.fps}")
        if 'fps_text' in got and abs(float(got['fps_text']) - float(plan.rate.fps)) > 0.01:
            problems.append(f"frame rate {got['fps_text']}, expected {float(plan.rate.fps):.3f}")
        if 'duration' in got and 'frames' not in got:
            want = plan.video_frames / float(plan.rate.fps)
            if abs(got['duration'] - want) > 1.0 / float(plan.rate.fps):
                problems.append(f"duration {got['duration']:.3f}s, expected {want:.3f}s")
        if 'samples' in got and got['samples'] != enc.total:
            problems.append(f"{got['samples']} audio samples, expected {enc.total}")
        if (got.get('width'), got.get('height')) != (cfg['w'], cfg['h']):
            problems.append(f"size {got.get('width')}x{got.get('height')}, expected {cfg['w']}x{cfg['h']}")
        if cfg['tctrack'] and got.get('timecode') not in (None, plan.embedded_timecode):
            problems.append(f"timecode track {got.get('timecode')}, expected {plan.embedded_timecode}")
        if problems:
            bad = j['stem'] + '.UNVERIFIED.mov'
            os.replace(j['mux_out'], bad)
            self._end_job('Failed check: ' + '; '.join(problems) + f'. File kept as {bad}', keep=False)
            return
        os.replace(j['mux_out'], j['final'])
        secs = time.time() - j['t0']
        note = (f'; timestamps corrected, TouchDesigner wrote {j["retimed"]}'
                if j.get('retimed') else '')
        if j.get('engine') == 'ffmpeg fast':
            note += ', ffmpeg fast'
        self._end_job(f'Done: {os.path.basename(j["final"])}  ({plan.video_frames:,} frames, '
                      f'{int(secs // 60)}:{int(secs % 60):02d}{note})', keep=False, progress=1.0)

    def _end_job(self, message, keep, progress=None):
        """Stop everything and clean up. keep=True leaves the video and LTC files."""
        j, self.job = self.job, None
        self.ownerComp.unstore('active_job')
        if j and j.get('orig_cook'):
            project.cookRate = j['orig_cook']
        try:
            self.child('mvo').par.record = 0
            self.child('mvo').par.pause = 0
        except Exception:
            pass
        if j:
            proc = j.get('proc')
            if proc and proc.poll() is None:
                # Still running (cancel, failure): stop the fast worker's ffmpeg
                # too. Never signal pids of processes that have exited: the
                # system may have given them to something else.
                self._kill_pids(j)
                proc.kill()
            for fh in (j.get('pcm'), j.get('log')):
                try:
                    if fh and not fh.closed:
                        fh.close()
                except Exception:
                    pass
            junk = [j.get('mux_out'), j.get('progress'), j.get('log_path')] + j.get('temp', [])
            if not keep:
                junk += [j['pcm_path'], j['video']]
            # Movie File Out may still hold the video open: OnFrame deletes
            # these once it reports the file closed.
            self.cleanup = self.cleanup + [f for f in junk if f and f not in self.cleanup]
            self._cleanup_t0 = time.time()
        if progress is not None:
            self._set('Progress', progress)
        self._status(message)
        if j and 'batch' in j:
            ok = message.startswith('Done')
            self._set_row_status(j['batch'], message)
            self.BatchResults.append((j['batch'], message, j['final'] if ok else None))
            if getattr(self, 'batch', None):
                run("op(args[0]).ext.LtcGenExt._next_batch()", self.ownerComp.path,
                    delayFrames=5, fromOP=self.ownerComp)
                return
            self._restore_batch_settings()
            n_ok = sum(1 for r in self.BatchResults if r[2])
            self._status(f'Batch finished: {n_ok}/{len(self.BatchResults)} rendered. Last: {message}')
        self._set('Rendering', False)
        self.Refresh(update_status=False)

    def _restore_batch_settings(self):
        for k, v in (getattr(self, '_batch_restore', None) or {}).items():
            setattr(self.pars, k, v)
        self._batch_restore = None
        self.ownerComp.unstore('batch_restore')
        # The restored values fire parameter callbacks next frame; keep the
        # batch result in Status instead of replacing it with "Ready".
        self._quiet_until = absTime.frame + 2

    # ------------------------------------------------------------------
    # Movie File Out state (Info CHOP, as in vCon)

    def _mvo_info(self, chan):
        info = self.ownerComp.op('mvo_info')
        if info is None:
            return None
        info.cook(force=True)
        c = info.chan(chan)
        return float(c[0]) if c is not None else None

    def _mvo_recording(self):
        a, t = self._mvo_info('active_records'), self._mvo_info('total_frames_written')
        return a is None or (a or 0) > 0 or (t or 0) > 0

    def _mvo_writing(self):
        a = self._mvo_info('active_records')
        return bool(a and a > 0)

    def _mvo_errors(self):
        try:
            return (self.child('mvo').errors() or '').strip()
        except Exception:
            return ''

    # ------------------------------------------------------------------
    # Batch list (panel): rows are snapshots of the settings

    #: Pages whose parameters make up a row (Setup stays global).
    BATCH_PAGES = ('Job', 'Video', 'Picture', 'Audio')

    def _rows(self):
        return self.ownerComp.fetch('batch_rows', [], search=False)

    def _store_rows(self, rows):
        self.ownerComp.store('batch_rows', rows)
        self._set('Batchcount', len(rows))
        self._write_batch_view()

    def _snapshot(self):
        snap = {}
        for page in self.ownerComp.customPages:
            if page.name not in self.BATCH_PAGES:
                continue
            for par in page.pars:
                if par.isPulse or par.readOnly:
                    continue
                snap[par.name] = par.eval()
        return snap

    def BatchAdd(self):
        """Add the current settings as a batch row (only if they are valid)."""
        if self._busy():
            return
        try:
            plan, audio, cfg = self._settings()
            name = self._filename(plan, cfg)
        except (ValueError, self.tc.TimecodeError) as exc:
            self._status(f'Cannot add to batch: {exc}')
            return
        rows = list(self._rows())
        s = plan.summary()
        rows.append({'settings': self._snapshot(), 'file': name + '.mov',
                     'rate': plan.rate.key + (' DF' if plan.drop_frame else ''),
                     'start': s['start'], 'duration': self.pars.Duration.eval(),
                     'preroll': self.pars.Preroll.eval(), 'codec': CODECS[cfg['codec']][0],
                     'size': f"{cfg['w']}x{cfg['h']}", 'status': 'queued'})
        self._store_rows(rows)
        self._status(f'Added to batch ({len(rows)} rows): {name}.mov')

    def BatchRemove(self):
        rows = list(self._rows())
        if rows and not self._busy():
            rows.pop()
            self._store_rows(rows)

    def BatchClear(self):
        if not self._busy():
            self._store_rows([])

    def BatchRender(self):
        rows = list(self._rows())
        if not rows or self._busy():
            return
        for r in rows:
            r['status'] = 'queued'
        self._store_rows(rows)
        self.RenderBatch([dict(r['settings'], _row=k) for k, r in enumerate(rows)])

    def _set_row_status(self, overrides, text):
        k = overrides.get('_row')
        rows = list(self._rows())
        if k is None or not 0 <= k < len(rows):
            return
        rows[k] = dict(rows[k], status=self._short_status(text))
        self._store_rows(rows)

    @staticmethod
    def _short_status(text):
        """Row status for the batch table; the full message stays in Status."""
        t = ' '.join(str(text).split())
        if t.startswith('Done'):
            return 'done'
        for prefix, short in (('Cannot render: ', 'invalid: '), ('Failed check: ', 'failed check: '),
                              ('Failed: ', 'failed: ')):
            if t.startswith(prefix):
                return (short + t[len(prefix):])[:70]
        return t[:70]

    def _write_batch_view(self):
        view = self.ownerComp.op('batch_view')
        if view is None:
            return
        view.clear()
        view.appendRow(['#', 'Status', 'Rate', 'Start', 'Duration', 'Pre-roll', 'Codec', 'Size', 'File'])
        for k, r in enumerate(self._rows(), 1):
            view.appendRow([k, r['status'], r['rate'], r['start'], r['duration'], r['preroll'],
                            r['codec'], r['size'], r['file']])

    # ------------------------------------------------------------------
    # Setup & Help (the ? button)

    def HelpText(self):
        """The Setup & Help text (printable; ShowHelp puts it in a dialog)."""
        info = self.CheckFfmpeg(force=True)
        py = self.ff.python_for_pip() or ('py' if os.name == 'nt' else 'python3')
        dest = self.ff.pylibs_dir()
        cmd = ' '.join(f'"{a}"' if ' ' in a else a for a in self.ff.install_command(py, dest))
        lines = ['AUDIO (ffmpeg): ' + ('READY' if info else 'NOT FOUND'), '']
        if info:
            lines += [f'ffmpeg {info.version}', f'  {info.path}', f'  found as: {info.source}',
                      f'ffprobe: {self.ffprobe or "not found (optional)"}']
        else:
            lines += ['LTC audio is added to the rendered video with ffmpeg,',
                      'which is not bundled. Install Now downloads it',
                      '(imageio-ffmpeg, about 30 MB) into a folder in your',
                      'user Library: no admin rights, nothing written into',
                      'TouchDesigner, and it survives TouchDesigner updates.',
                      '', 'Also found automatically: Homebrew / MacPorts /',
                      'winget / Chocolatey / Scoop installs, and copies',
                      'downloaded by vCon (Setup page: ffmpeg Source).']
        lines += ['', 'Install folder:', f'  {dest}', '',
                  'Manual install (Terminal, or cmd / PowerShell):', f'  {cmd}']
        return '\n'.join(lines), cmd, info is not None

    def ShowHelp(self):
        text, cmd, ready = self.HelpText()
        buttons = ['Check Again' if ready else 'Install Now', 'Copy Command', 'Close']
        choice = ui.messageBox('LTC File Gen: Setup & Help', text, buttons=buttons)
        if choice == 0:
            if ready:
                self.CheckFfmpeg(force=True)
            else:
                self.InstallFfmpeg()
        elif choice == 1:
            ui.clipboard = cmd
            self._set('Ffmpegstatus', 'Install command copied to the clipboard')

    def ExportTox(self, path=None):
        """Save the component as a .tox without this machine's state in it."""
        if self.job:
            raise RuntimeError('cannot export while rendering')
        path = path or os.path.join(_engine_root(self.ownerComp), 'td', 'LTCfileGen.tox')
        self.child('mvo').par.file = ''
        for name, value in (('Ffmpegready', False), ('Ffmpegstatus', ''), ('Status', ''),
                            ('Summary', ''), ('Progress', 0.0), ('Rendering', False)):
            self._set(name, value)
        for key in ('active_job', 'batch_restore'):
            self.ownerComp.unstore(key)
        rows = list(self._rows())
        self._store_rows([])
        self.ownerComp.save(path)
        self._store_rows(rows)
        self.Refresh()
        self.CheckFfmpeg(force=True)
        return path

    # ------------------------------------------------------------------
    # Parameter callbacks (from the Parameter Execute DAT)

    def OnPulse(self, par):
        n = par.name
        if n == 'Render':
            if self._busy():
                self._status('Busy: a render or batch is running')
            else:
                self.Render()
        elif n == 'Cancel':
            self.Cancel()
        elif n == 'Checkffmpeg':
            self.CheckFfmpeg(force=True)
        elif n == 'Installffmpeg':
            self.InstallFfmpeg()
        elif n == 'Help':
            self.ShowHelp()
        elif n == 'Batchadd':
            self.BatchAdd()
        elif n == 'Batchremove':
            self.BatchRemove()
        elif n == 'Batchclear':
            self.BatchClear()
        elif n == 'Batchrender':
            self.BatchRender()

    def OnParChange(self, par):
        if par.name in ('Status', 'Progress', 'Summary', 'Ffmpegstatus', 'Ffmpegready', 'Rendering', 'Batchcount'):
            return
        if par.name in ('Ffmpegsource', 'Ffmpegpath', 'Shareffmpeg'):
            self.CheckFfmpeg(force=True)
            return
        if self.job:
            return                            # settings are fixed while rendering
        self.Refresh(update_status=absTime.frame > getattr(self, '_quiet_until', -1))
