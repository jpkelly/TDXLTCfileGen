"""Build the component's panel from TouchDesigner's Basic Widgets.

Run in the development project (textport):

    exec(open(project.folder + '/tools/td_build_ui.py', encoding='utf-8').read())
    build_ui()

Rebuilds, each time:

* ui_body/ui_form: the main settings, one widget per setting, each bound
  two-way to the component's custom parameter (the parameters stay the
  single source of truth: batch rows, defaults and scripting use them);
* settings/: the "More settings" window, TouchDesigner's own parameter
  dialog for the Picture, Audio and Setup pages, wide enough not to clip;
* ui_batch: a full-width batch section with TouchDesigner's lister;
* the type, spacing and colours of the whole panel.

It checks that every editable setting is either on the form or in the
settings window. Not importable as a module: TouchDesigner's names exist
only in the textport.
"""

import os

WIDGETS = os.path.join(app.samplesFolder, 'Palette', 'UI', 'Basic Widgets')
LISTER = os.path.join(app.samplesFolder, 'Palette', 'UI', 'lister.tox')
APP_NAME = 'LTC File Generator'

# ---- design
FONT = 'Verdana'
SIZE_TITLE, SIZE_BUTTON, SIZE_TEXT, SIZE_LABEL = 20, 14, 13, 11
MARGIN, GUTTER = 16, 16
ROW_H, ROW_GAP, GROUP_GAP, LABEL_W, FORM_W = 30, 6, 14, 128, 440
TOP_H = 56
BG = (0.08, 0.08, 0.09)
BAR_BG = (0.13, 0.13, 0.15)
TEXT = (0.92, 0.92, 0.92)
TEXT_DIM = (0.58, 0.58, 0.6)
LABEL = (0.62, 0.62, 0.65)
GREEN = (0.2, 0.62, 0.32)
TRACK = (0.17, 0.17, 0.19)
FIELD = (0.19, 0.19, 0.21)
FIELD_HOVER = (0.25, 0.25, 0.28)
MENU_BG = (0.15, 0.15, 0.17)
ACCENT = (0.2, 0.44, 0.7)
ICON_FONT = 'Material Design Icons'      # the folder field's browse icon
SETTINGS_PAGES = ('Picture', 'Audio', 'Setup')
SETTINGS_SIZE = (780, 620)

#: (widget, name, label, parameter(s)); ('gap',) separates groups, ('fill',)
#: takes the spare height (so the last row sits at the bottom whether or not
#: the WIDTH / HEIGHT row is shown).
FORM = [
    ('dropDownMenu', 'rate', 'FRAME RATE', 'Rate'),
    ('buttonToggle', 'dropframe', 'DROP FRAME', 'Dropframe'),
    ('gap',),
    ('fieldString', 'start', 'START', 'Start'),
    ('fieldString', 'duration', 'DURATION', 'Duration'),
    ('fieldString', 'preroll', 'PRE-ROLL', 'Preroll'),
    ('gap',),
    ('dropDownMenu', 'filetype', 'FILE TYPE', 'Output'),
    ('dropDownMenu', 'codec', 'CODEC', 'Codec'),
    ('dropDownMenu', 'engine', 'ENGINE', 'Engine'),
    ('dropDownMenu', 'size', 'SIZE', 'Resolution'),
    ('int2', 'customsize', 'WIDTH / HEIGHT', ('Customw', 'Customh')),
    ('gap',),
    ('fieldFolderBrowser', 'folder', 'OUTPUT FOLDER', 'Outputfolder'),
    ('fieldString', 'filename', 'FILE NAME', 'Filename'),
    ('buttonToggle', 'overwrite', 'OVERWRITE', 'Overwrite'),
    ('fill',),
    ('buttonMomentary', 'more', 'More settings…  (picture, audio, ffmpeg)', None),
]

#: Toggle parameters, so the expressions return True/False.
DISPLAY = {'customsize': "parent.LtcGen.par.Resolution.eval() == 'custom'"}
_MOVIE = "parent.LtcGen.par.Output.eval() != 'wav'"           # video settings apply
ENABLE = {'dropframe': "parent.LtcGen.par.Rate.eval() in ('29.97', '59.94')",
          'codec': _MOVIE, 'engine': _MOVIE, 'size': _MOVIE, 'customsize': _MOVIE}

#: Settings that need no control: read-only state and the pulses behind the
#: panel's buttons.
NOT_EDITABLE = {'Summary', 'Estimate', 'Outputfile', 'Lastfile', 'Progress', 'Status', 'Rendering', 'Render', 'Cancel',
                'Batchadd', 'Batchremove', 'Batchclear', 'Batchrender', 'Batchcount'}

STATUS_COLOUR = ("(0.95, 0.45, 0.4) if parent.LtcGen.par.Status.eval().startswith(('Cannot', 'Failed', 'Check')) "
                 "else (0.45, 0.85, 0.5) if parent.LtcGen.par.Status.eval().startswith(('Done', 'Batch finished')) "
                 "else (0.92, 0.92, 0.92)")


# ----------------------------------------------------------------------
# helpers

def _rgb(o, prefix, rgb, alpha=None):
    for ch, v in zip('rgb', rgb):
        getattr(o.par, prefix + ch).val = v
    if alpha is not None:
        getattr(o.par, 'bgalpha' if prefix == 'bgcolor' else prefix + 'alpha').val = alpha


def _palette_widget(parent, tox_path, name):
    """A copy of the widget inside a palette .tox (as vCon's build does)."""
    scratch = parent.create(containerCOMP, '_scratch')
    try:
        wrapper = scratch.loadTox(tox_path)
        inner = next((c for c in wrapper.children if c.name == wrapper.name or c.isPanel), wrapper)
        return parent.copy(inner, name=name)
    finally:
        scratch.destroy()


def _container(parent, name, w=100, h=100, align='none'):
    c = parent.op(name) or parent.create(containerCOMP, name)
    c.par.w, c.par.h = w, h
    c.par.align = align
    c.par.bgalpha = 0
    return c


def _text(parent, name, order, h, size, colour, text=None, text_expr=None):
    t = parent.op(name) or parent.create(textCOMP, name)
    t.par.alignorder = order
    t.par.hmode, t.par.vmode = 'fill', 'fixed'
    t.par.h = h
    t.par.font = FONT
    t.par.fontsize = size
    t.par.alignx, t.par.aligny = 'left', 'center'
    t.par.bgalpha = 0
    if text_expr:
        t.par.text.expr = text_expr
    elif text is not None:
        t.par.text = text
    _rgb(t, 'fontcolor', colour)
    return t


def _set(w, name, value):
    p = getattr(w.par, name, None)
    if p is not None:
        try:
            p.val = value
        except Exception:
            pass


def _set_rgb(w, prefix, rgb):
    for ch, v in zip('rgb', rgb):
        _set(w, prefix + ch, v)


def _style_widget(w, kind):
    """One type scale, one field style and quiet labels for every widget."""
    for p in w.customPars:
        n = p.name
        if n.endswith('fontsizemode') and 'panelunits' in (p.menuNames or []):
            p.val = 'panelunits'
        elif n in ('Font', 'Menufont', 'Labelfont') or (n == 'Buttonfont' and kind != 'fieldFolderBrowser'):
            p.val = FONT
    if kind == 'fieldFolderBrowser':
        _set(w, 'Buttonfont', ICON_FONT)
    _set(w, 'Labelfontsize', SIZE_LABEL)
    for n in ('Stringfieldfontsize', 'Numericfieldfontsize', 'Menufontsize', 'Menubuttonfontsize'):
        _set(w, n, SIZE_TEXT)
    _set(w, 'Buttonfontsize', 16 if kind == 'fieldFolderBrowser' else SIZE_TEXT)
    w.par.bgalpha = 0                                  # no band behind the row
    _set(w, 'Labelbgcolora', 0)
    _set_rgb(w, 'Labelfontcolor', LABEL)
    _set(w, 'Labelpadding1', 0)
    # fields: dark with light text, like the rest of the panel
    _set_rgb(w, 'Stringfieldbgcolor', FIELD)
    _set_rgb(w, 'Stringfieldfontcolor', TEXT)
    _set_rgb(w, 'Numericfieldbgcolor', FIELD)
    _set_rgb(w, 'Numericfieldfontcolor', TEXT)
    _set_rgb(w, 'Menubuttonofffacecolor', FIELD)
    _set_rgb(w, 'Menubuttononfacecolor', FIELD_HOVER)
    _set_rgb(w, 'Menufontoffcolor', TEXT)
    _set_rgb(w, 'Menufontoncolor', (1, 1, 1))
    _set_rgb(w, 'Menubgcolor', MENU_BG)
    _set_rgb(w, 'Menurollcolor', FIELD_HOVER)
    _set_rgb(w, 'Menuselectcolor', ACCENT)
    # buttons and toggles: field colour when off, accent when on
    _set_rgb(w, 'Buttonofffacecolor', FIELD)
    _set_rgb(w, 'Buttonofffontcolor', (0.82, 0.82, 0.84))
    _set_rgb(w, 'Buttononfacecolor', ACCENT)
    _set_rgb(w, 'Buttononfontcolor', (1, 1, 1))
    # disabled: fade into the background rather than a grey band
    _set_rgb(w, 'Disablecolor', BG)
    _set(w, 'Disablecolora', 0.6)


def _menu(widget, par):
    widget.par.Menunames = ' '.join(par.menuNames)
    widget.par.Menulabels = ' '.join(f"'{l}'" if ' ' in l else l for l in par.menuLabels)


def _style_button(b, size=SIZE_BUTTON):
    _set(b, 'font', FONT)
    _set(b, 'fontsize', size)


# ----------------------------------------------------------------------

def _build_settings(comp, body):
    old = comp.op('settings')
    params = body.op('ui_params') or (old.op('ui_settings/ui_params') if old else None)
    if params is None:
        raise RuntimeError('ui_params (the parameter dialog) is missing')
    if comp.op('settings_new'):
        comp.op('settings_new').destroy()           # left by a failed run
    settings = comp.create(baseCOMP, 'settings_new')
    panel = _container(settings, 'ui_settings', *SETTINGS_SIZE, align='verttb')
    _rgb(panel, 'bgcolor', (0.25, 0.25, 0.25), 1)
    moved = panel.copy(params, name='ui_params')
    moved.par.pagescope = ' '.join(SETTINGS_PAGES)
    moved.par.hmode, moved.par.vmode = 'fill', 'fill'
    win = settings.create(windowCOMP, 'win')
    win.par.winop = panel.name
    win.par.title = f'{APP_NAME}: more settings'
    win.par.size = 'custom'
    win.par.winw, win.par.winh = SETTINGS_SIZE
    win.par.justifyh, win.par.justifyv = 'center', 'center'
    if params.parent() == body:
        params.destroy()
    if old:
        old.destroy()
    settings.name = 'settings'
    settings.nodeX, settings.nodeY = 300, -1000
    return settings


def _build_form(comp, body):
    if body.op('ui_form'):
        body.op('ui_form').destroy()
    form = _container(body, 'ui_form', FORM_W, 600, align='verttb')
    form.par.hmode, form.par.vmode = 'fixed', 'fill'
    form.par.alignorder = 0
    form.par.spacing = ROW_GAP
    form.par.enable.expr = 'not parent.LtcGen.par.Rendering.eval()'
    form.par.opacity.expr = '0.45 if parent.LtcGen.par.Rendering.eval() else 1'      # locked while rendering
    bound, height = set(), 0
    for i, row in enumerate(FORM):
        if row[0] in ('gap', 'fill'):
            g = _container(form, f'gap{i}', 10, GROUP_GAP - ROW_GAP)
            g.par.hmode, g.par.vmode, g.par.alignorder = 'fill', 'fixed', i
            if row[0] == 'fill':
                g.par.vmode = 'fill'
            height += GROUP_GAP
            continue
        kind, name, label, pars = row
        w = _palette_widget(form, os.path.join(WIDGETS, kind + '.tox'), 'w_' + name)
        w.par.alignorder = i
        w.par.hmode, w.par.vmode = 'fill', 'fixed'
        w.par.h = ROW_H
        height += ROW_H + ROW_GAP
        _style_widget(w, kind)
        if kind == 'buttonMomentary':
            w.par.Widgetlabel = ''
            w.par.Labeldisplay = False
            w.par.Buttonofflabel = w.par.Buttononlabel = label
            w.par.Buttonfontbold = False
            w.par.Buttonhorizontalmode = 'fill'
            w.par.Offtoonscript0 = 'parent.LtcGen.ext.LtcGenExt.ShowSettings()'
        else:
            w.par.Widgetlabel = label
            w.par.Labelwidth = LABEL_W
            names = pars if isinstance(pars, tuple) else (pars,)
            for k, pname in enumerate(names):
                if kind == 'dropDownMenu':
                    _menu(w, getattr(comp.par, pname))
                getattr(w.par, f'Value{k}').bindExpr = f'parent.LtcGen.par.{pname}'
                bound.add(pname)
        if name in DISPLAY:
            w.par.display.expr = DISPLAY[name]
        if name in ENABLE:
            w.par.enable.expr = ENABLE[name]
        w.nodeX, w.nodeY = 0, -i * 120
    return form, bound, height - ROW_GAP


#: batch_view's columns and their widths in the batch list (File stretches).
BATCH_COLUMNS = [('#', 30), ('Status', 134), ('Rate', 76), ('Start', 108), ('Duration', 108),
                 ('Pre-roll', 108), ('Codec', 128), ('Size', 96), ('File', 200)]

HELP_ICON = 'chr(0xF0625)'                      # Material Design help-circle-outline
HELP_READY = (0.0, 0.3666667, 0.5)               # vCon's help button


RED = (0.5, 0.17, 0.17)
BLUE = (0.2, 0.32, 0.5)


def _widget_button(parent, name, label, order, width, face, script, enable=None, icon=False):
    """A Basic Widgets button: every button in the panel is one of these, so
    they share one look and height."""
    if parent.op(name) is not None:
        parent.op(name).destroy()
    w = _palette_widget(parent, os.path.join(WIDGETS, 'buttonMomentary.tox'), name)
    w.par.alignorder = order
    w.par.hmode, w.par.vmode = 'fixed', 'fill'
    w.par.w = width
    w.par.bgalpha = 0
    w.par.Widgetlabel = ''
    w.par.Labeldisplay = False
    w.par.Labelwidth = 1
    if icon:
        w.par.Buttonofflabel.expr = w.par.Buttononlabel.expr = label
        w.par.Buttonfont = ICON_FONT
        w.par.Buttonfontsize = 24
    else:
        w.par.Buttonofflabel = w.par.Buttononlabel = label
        w.par.Buttonfont = FONT
        w.par.Buttonfontsize = SIZE_BUTTON
        w.par.Buttonfontbold = False
    w.par.Buttonhorizontalmode = 'fill'
    for k, ch in enumerate('rgb'):
        par = getattr(w.par, 'Buttonofffacecolor' + ch)
        if isinstance(face[k], str):
            par.expr = face[k]
        else:
            par.val = face[k]
    _set_rgb(w, 'Buttononfacecolor', face if not isinstance(face[0], str) else ACCENT)
    _set_rgb(w, 'Buttonofffontcolor', (0.93, 0.93, 0.93))
    _set_rgb(w, 'Buttononfontcolor', (1, 1, 1))
    w.par.Offtoonscript0 = script
    if enable:
        w.par.enable.expr = enable
    return w


def _build_buttons(comp, batch):
    """Render, Cancel and help in the top bar; the batch buttons."""
    top = comp.op('ui_top')
    for name in ('btn_render', 'btn_cancel', 'btn_help', 'btn_help1', 'w_help'):
        if top.op(name) is not None:
            top.op(name).destroy()
    busy = 'parent.LtcGen.par.Rendering.eval()'
    ready = 'parent.LtcGen.par.Ffmpegready.eval()'
    pulse = 'abs((absTime.frame % 60) - 30) / 30'
    _widget_button(top, 'btn_render', 'Render', 1, 120, GREEN, 'parent.LtcGen.par.Render.pulse()', f'not {busy}')
    _widget_button(top, 'btn_cancel', 'Cancel', 2, 100, RED, 'parent.LtcGen.par.Cancel.pulse()', busy)
    # vCon's help button: circled ? on teal, pulsing red while ffmpeg is missing
    _widget_button(top, 'btn_help', HELP_ICON, 3, 44,
                   (f'{HELP_READY[0]} if {ready} else 0.4 + 0.5 * {pulse}',
                    f'{HELP_READY[1]} if {ready} else 0.1',
                    f'{HELP_READY[2]} if {ready} else 0.1'),
                   'parent.LtcGen.par.Help.pulse()', icon=True)
    bar = batch.op('ui_batch_bar')
    n = 'parent.LtcGen.par.Batchcount.eval()'
    for name, label, order, width, face, action, enable in (
            ('btn_badd', 'Add current', 1, 120, BLUE, 'Batchadd', f'not {busy}'),
            ('btn_bremove', 'Remove last', 2, 120, FIELD_HOVER, 'Batchremove', f'{n} > 0 and not {busy}'),
            ('btn_bclear', 'Clear', 3, 80, FIELD_HOVER, 'Batchclear', f'{n} > 0 and not {busy}'),
            ('btn_brender', 'Render batch', 4, 130, GREEN, 'Batchrender', f'{n} > 0 and not {busy}')):
        _widget_button(bar, name, label, order, width, face, f'parent.LtcGen.par.{action}.pulse()', enable)
    # the buttons call their actions themselves: the old click router goes
    if comp.op('ui_exec') is not None:
        comp.op('ui_exec').destroy()


def _style_lister(batch, lister):
    cfg = batch.op('listerConfig')
    looks = {'master': ((0.12, 0.12, 0.14), (0.86, 0.86, 0.88), 1.0),
             'header': ((0.17, 0.17, 0.19), LABEL, 1.0),
             'headerRoll': ((0.21, 0.21, 0.24), TEXT, 1.0),
             'rowRoll': ((0.22, 0.22, 0.25), TEXT, 1.0),
             'rowSelect': (ACCENT, (1, 1, 1), 0.45)}
    for name, (bg, fg, alpha) in looks.items():
        t = cfg.op(name)
        if t is None:
            continue
        t.par.font = FONT
        t.par.fontsize = 10 if name.startswith('header') else 11
        _rgb(t, 'bgcolor', bg, alpha)
        _rgb(t, 'fontcolor', fg)
    # one column per batch_view header: fixed widths, the file name takes the rest
    cd = cfg.op('colDefine')
    attrs = [cd[r, 0].val for r in range(cd.numRows)]
    cd.clear()
    for a in attrs:
        cd.appendRow([a])
    for header, width in BATCH_COLUMNS:
        col = {'column': header, 'columnLabel': header, 'sourceData': header, 'sourceDataMode': 'string',
               'topFill': 'BEST', 'width': width, 'stretch': 1 if header == 'File' else 0,
               'sizable': 1, 'editable': 0, 'selectRow': 1,
               'justify': 'CENTER' if header == '#' else 'CENTERLEFT',
               'draggable': 0, 'clickOnDrag': 0, 'fontBold': 0, 'fontItalic': 0}
        cd.appendCol([col.get(a, '') for a in attrs])
    lister.par.Autodefinecols = False
    _rgb(lister, 'bgcolor', (0.12, 0.12, 0.14), 1)
    lister.par.hscrollbar = False
    lister.par.scrollbarthickness = 8
    lister.par.scrolloverlay = 'on'
    lister.par.vscrollbar.expr = 'parent.LtcGen.par.Batchcount.eval() > 5'       # only when rows overflow
    lister.par.Refresh.pulse()


def _build_batch(comp, body):
    """Full-width batch section under the body (moved out of ui_right)."""
    nested = body.op('ui_right/ui_batch')
    if nested is not None:
        batch = comp.copy(nested, name='ui_batch')
        nested.destroy()
    else:
        batch = comp.op('ui_batch')
    batch.par.alignorder = 2
    batch.par.hmode, batch.par.vmode = 'fill', 'fill'
    batch.par.marginl = batch.par.marginr = MARGIN
    batch.par.marginb = MARGIN
    batch.par.margint = 0
    batch.par.spacing = 6
    bar = batch.op('ui_batch_bar')
    bar.par.h = 32
    bar.par.spacing = 6
    title = bar.op('ui_batch_title')
    title.par.font, title.par.fontsize = FONT, 15
    n = 'parent.LtcGen.par.Batchcount.eval()'
    title.par.text.expr = (f"'Batch' + ('   (' + str({n}) + (' file)' if {n} == 1 else ' files)') if {n} "
                           f"else '   \u00b7   set up a file above, then Add current')")

    old_list = batch.op('ui_batch_list')
    if old_list:
        old_list.destroy()
    lister = _palette_widget(batch, LISTER, 'ui_batch_list')
    lister.par.display = True                     # off in the palette copy
    lister.par.alignorder = 1
    lister.par.hmode, lister.par.vmode = 'fill', 'fill'
    lister.par.Inputtabledat.expr = "parent.LtcGen.op('batch_view')"
    lister.par.Inputtablehasheaders = True
    _style_lister(batch, lister)
    return batch


def _layout(comp, body, form_h):
    _rgb(comp, 'bgcolor', BG, 1)
    top = comp.op('ui_top')
    top.par.h = TOP_H
    top.par.marginl = top.par.marginr = MARGIN
    top.par.margint = top.par.marginb = 8
    top.par.spacing = 8
    _rgb(top, 'bgcolor', BAR_BG, 1)
    title = top.op('ui_title')
    title.par.text = APP_NAME
    title.par.font, title.par.fontsize = FONT, SIZE_TITLE

    body.par.vmode = 'fixed'
    body.par.h = form_h + 2 * MARGIN
    body.par.marginl = body.par.marginr = body.par.margint = body.par.marginb = MARGIN
    body.par.spacing = GUTTER

    right = body.op('ui_right')
    right.par.spacing = 8
    for name in ('ui_info', 'ui_info_callbacks'):
        if right.op(name) is not None:
            right.op(name).destroy()
    # preview at the top (16:9, aligned with the form), status at the bottom
    # (aligned with the form's last row), space between
    preview = right.op('ui_preview')
    preview.par.alignorder = 0
    preview.par.vmode = 'fixed'
    preview.par.h.expr = 'int(me.width * 9 / 16)'
    _text(right, 'ui_summary', 1, 20, SIZE_TEXT, TEXT_DIM, text_expr='parent.LtcGen.par.Summary.eval()')
    _text(right, 'ui_output', 2, 20, SIZE_TEXT, TEXT_DIM, text_expr='parent.LtcGen.par.Outputfile.eval()')
    spacer = right.op('ui_spacer') or _container(right, 'ui_spacer', 10, 10)
    spacer.par.hmode, spacer.par.vmode, spacer.par.alignorder = 'fill', 'fill', 2.5
    # status line, with "Show in Finder" at its end once a file is finished
    if right.op('ui_status') is not None:
        right.op('ui_status').destroy()
    row = right.op('ui_statusrow') or _container(right, 'ui_statusrow', 10, 26, align='horizlr')
    row.par.hmode, row.par.vmode, row.par.alignorder = 'fill', 'fixed', 3
    row.par.spacing = 8
    status = _text(row, 'ui_status', 0, 26, SIZE_TEXT, TEXT,
                   text_expr="parent.LtcGen.par.Status.eval() + ('   \u00b7   ' + parent.LtcGen.par.Estimate.eval() "
                             "if parent.LtcGen.par.Status.eval() == 'Ready' else '')")
    status.par.vmode = 'fill'
    for k, ch in enumerate('rgb'):
        getattr(status.par, 'fontcolor' + ch).expr = f'({STATUS_COLOUR})[{k}]'
    reveal = _widget_button(row, 'btn_reveal', 'Show in Finder', 1, 150, FIELD_HOVER,
                            'parent.LtcGen.ext.LtcGenExt.ShowInFinder()')
    reveal.par.Buttonofflabel.expr = reveal.par.Buttononlabel.expr = 'parent.LtcGen.ext.LtcGenExt.RevealLabel'
    reveal.par.display.expr = ("parent.LtcGen.par.Lastfile.eval() != '' "
                               "and not parent.LtcGen.par.Rendering.eval()")
    prog = right.op('ui_progress')
    prog.par.alignorder = 4
    prog.par.h = 6
    _rgb(prog, 'bgcolor', TRACK, 1)
    bar = prog.op('bar')
    for k, ch in enumerate('rgb'):
        getattr(bar.par, 'bgcolor' + ch).expr = (f'{GREEN[k]} if parent.LtcGen.par.Rendering.eval() '
                                                f'else {TRACK[k]}')


def _ensure_readonly(comp, name, after):
    """A read-only text setting on the Job page, filled in by Refresh."""
    if not hasattr(comp.par, name):
        page = next(p for p in comp.customPages if p.name == 'Job')
        par = page.appendStr(name, label=name)[0]
        par.readOnly = True
        names = [p.name for p in page.pars]
        names.remove(name)
        names.insert(names.index(after) + 1, name)
        page.sort(*names)


def _ensure_output_menu(comp):
    """File type: Movie, Movie + WAV or WAV only (Job page, after Overwrite)."""
    if not hasattr(comp.par, 'Output'):
        page = next(p for p in comp.customPages if p.name == 'Job')
        par = page.appendMenu('Output', label='File Type')[0]
        par.menuNames = ['movie', 'moviewav', 'wav']
        par.menuLabels = ['Movie', 'Movie + WAV', 'WAV only']
        par.default = par.val = 'movie'
        names = [p.name for p in page.pars]
        names.remove('Output')
        names.insert(names.index('Overwrite') + 1, 'Output')
        page.sort(*names)


def build_ui(comp_path='/project1/LTCfileGen'):
    comp = op(comp_path)
    if comp.par.Rendering.eval():
        raise RuntimeError('wait for the render to finish')
    _ensure_readonly(comp, 'Estimate', 'Summary')       # render time and engine
    _ensure_readonly(comp, 'Outputfile', 'Estimate')    # the file a render writes
    _ensure_readonly(comp, 'Lastfile', 'Outputfile')    # last finished file (Show in Finder)
    _ensure_output_menu(comp)
    body = comp.op('ui_body')
    _build_settings(comp, body)
    form, bound, form_h = _build_form(comp, body)
    batch = _build_batch(comp, body)
    _build_buttons(comp, batch)
    _layout(comp, body, form_h)

    in_window = {p.name for p in comp.customPars if p.page.name in SETTINGS_PAGES}
    missing = [p.name for p in comp.customPars
               if p.name not in bound | in_window | NOT_EDITABLE and not p.readOnly]
    if missing:
        raise RuntimeError(f'not on the form or in the settings window: {missing}')
    # network: the panel's parts in one row (inside the Panel annotation)
    for name, x in (('ui_top', 0), ('ui_body', 200), ('ui_batch', 400), ('settings', 600)):
        comp.op(name).nodeX, comp.op(name).nodeY = x, -760
    comp.op('batch_view').nodeX, comp.op('batch_view').nodeY = 800, -720
    comp.ext.LtcGenExt.Refresh(update_status=False)
    print(f'build_ui: {len(bound)} settings on the form, {len(in_window)} in the settings window; '
          f'form {form_h}px tall')
    return form
