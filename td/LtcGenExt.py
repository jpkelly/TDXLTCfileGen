"""LTC File Gen: TouchDesigner extension.

Drives TouchDesigner only: parameters, the picture, stop-frame recording,
the background ffmpeg mux and the read-back check. All timecode, LTC and
ffmpeg logic lives in the plain-Python engine in ../src/ltcfilegen, which is
tested outside TouchDesigner.

Render loop (one step per cooked frame, from the frame-start Execute DAT):
  arm     record=1, pause=1, let Movie File Out open the file
  record  frame i: set picture, append LTC samples, pulse Add Frame
  close   record=0, wait until Movie File Out reports the file complete
  mux     ffmpeg copies the video and adds the LTC track (background)
  verify  read the finished file back; fail loudly on any mismatch
"""

import importlib
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
#: the loop follows the display refresh (about 118/s on a 120 Hz MacBook Pro
#: display, about 60/s on a 60 Hz display).
EST_FRAMES_PER_SECOND = 60.0

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
        # Reload in dependency order so edits on disk take effect on re-init.
        for m in (ltcfilegen.timecode, ltcfilegen.job, ltcfilegen.ltc, ltcfilegen.ffmpeg):
            importlib.reload(m)
        self.tc, self.jobs, self.ltc, self.ff = (ltcfilegen.timecode, ltcfilegen.job,
                                                 ltcfilegen.ltc, ltcfilegen.ffmpeg)
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
        pid = stale.get('pid')
        if pid:
            try:
                name = subprocess.run(['ps', '-p', str(pid), '-o', 'comm='], capture_output=True,
                                      text=True, timeout=5).stdout if os.name != 'nt' else ''
                if 'ffmpeg' in name:
                    os.kill(pid, 15)
            except Exception:
                pass
        stem = stale['stem']
        self.cleanup = [stale['video'], stale['pcm'], stem + '.muxing.mov',
                        stem + '.mux.progress', stem + '.mux.log']
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
        est = plan.video_frames / EST_FRAMES_PER_SECOND
        s = plan.summary()
        self._set('Summary', f"{s['first']} to {s['end']}  |  {plan.video_frames:,} frames  |  "
                             f"{int(secs // 3600)}:{int(secs % 3600 // 60):02d}:{secs % 60:05.2f}  |  "
                             f"render about {max(1, round(est / 60))} min")
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
        ltc_text, video_text = plan.ltc_text(i), plan.video_text(i)
        high = plan.div == 2
        main_is_video = high and cfg['main'] == 'video'
        main = video_text if main_is_video else ltc_text
        sub = ''
        if high and cfg['secondary']:
            sub = f'LTC {ltc_text}' if main_is_video else f'VIDEO {video_text}'
        count = f'-{plan.remaining_text(i)}' if cfg['countdown'] else ''
        for name, text in (('text_main', main), ('text_sub', sub), ('text_count', count)):
            t = self.child(name)
            if t.par.text.eval() != text:
                t.par.text = text
        vlabel = plan.video_label(i)
        flash = cfg['flash'] and vlabel[3] == 0 and not preview
        bg, fg = (cfg['fg'], cfg['bg']) if flash else (cfg['bg'], cfg['fg'])
        g = self.child('glsl_picture')
        g.par.vec0valuex, g.par.vec0valuey, g.par.vec0valuez = bg
        g.par.vec1valuex, g.par.vec1valuey, g.par.vec1valuez = fg
        g.par.vec2valuey = i
        g.par.vec3valuey = vlabel[3]
        g.par.vec3valuez = plan.rate.nominal

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
                    'proc': None, 'log': None}
        self._set('Progress', 0.0)
        self.job['orig_cook'] = project.cookRate
        self.ownerComp.store('active_job', {'video': video, 'pcm': pcm_path, 'stem': stem,
                                            'orig_cook': project.cookRate})
        project.cookRate = RENDER_COOK_RATE
        self._set('Rendering', True)
        mvo.par.record = 1
        mvo.par.pause = 1
        self._status(f'Rendering {os.path.basename(final)}')

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
                proc.kill()
            for fh in (j.get('pcm'), j.get('log')):
                try:
                    if fh and not fh.closed:
                        fh.close()
                except Exception:
                    pass
            junk = [j.get('mux_out'), j.get('progress'), j.get('log_path')]
            if not keep:
                junk += [j['pcm_path'], j['video']]
            # Movie File Out may still hold the video open: OnFrame deletes
            # these once it reports the file closed.
            self.cleanup = [f for f in junk if f]
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
