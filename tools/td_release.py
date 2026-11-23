"""Build the self-contained component, inside TouchDesigner.

Run it in the development project (saved in the repository folder), in the
textport:

    exec(open(project.folder + '/tools/td_release.py', encoding='utf-8').read())
    release_tox()

It writes td/LTCfileGen.tox from /project1/LTCfileGen with:

* the code baked in: every file-synced DAT (the extension, the shader and
  the engine modules in ./engine) keeps its text and loses its file link, so
  the component works in any project, anywhere;
* every setting at its default and no state from this machine (output
  folder, ffmpeg choice, status, batch list).

Then make the ready-to-open project with tools/release_toe.py (outside
TouchDesigner).

link_to_repo() does the reverse for development: it links a component's
code back to the repository files (td/, src/ltcfilegen/), so edits on disk
take effect. The project must be saved in the repository folder.

Not importable as a module: TouchDesigner's names (op, project, DAT) exist
only in the textport and DAT namespace.
"""

import os


def release_tox(comp_path='/project1/LTCfileGen', out=None):
    src = op(comp_path)
    if src.par.Rendering.eval():
        raise RuntimeError('wait for the render to finish')
    out = out or os.path.join(project.folder, 'td', 'LTCfileGen.tox')
    # Copy under its own name (a .tox keeps it) in a scratch parent.
    scratch = op('/').create(baseCOMP, 'ltcfilegen_release_build')
    tmp = scratch.copy(src, name='LTCfileGen')
    baked = []
    try:
        for dat in tmp.findChildren(type=DAT):
            sync = getattr(dat.par, 'syncfile', None)
            if sync is not None and sync.eval() and dat.par.file.eval():
                text = dat.text
                sync.val = False
                dat.par.file = ''
                dat.text = text
                baked.append(dat.path[len(tmp.path) + 1:])
        for p in tmp.customPars:
            if not p.isPulse and p.mode == ParMode.CONSTANT:
                p.val = p.default
        tmp.op('mvo').par.file = ''
        for key in list(tmp.storage.keys()):
            tmp.unstore(key)
        tmp.ext.LtcGenExt._write_batch_view()
        for name in ('Summary', 'Status', 'Ffmpegstatus'):
            getattr(tmp.par, name).val = ''
        tmp.par.Ffmpegready.val = False
        tmp.par.Progress.val = 0.0
        tmp.save(out)
    finally:
        scratch.destroy()
    print(f'release_tox: wrote {out}; baked in {len(baked)} files: {", ".join(sorted(baked))}')
    return out


#: Code DATs and their repository files (paths relative to the project).
LINKS = {'LtcGenExt': 'td/LtcGenExt.py', 'glsl_picture_pixel': 'td/glsl/picture.glsl'}


def link_to_repo(comp_path='/project1/LTCfileGen'):
    comp = op(comp_path)
    pairs = [(comp.op(name), rel) for name, rel in LINKS.items()]
    pairs += [(d, 'src/ltcfilegen/%s.py' % ('__init__' if d.name == 'init' else d.name))
              for d in comp.op('engine').children if d.isDAT]
    for dat, rel in pairs:
        if not os.path.isfile(os.path.join(project.folder, rel)):
            raise RuntimeError(f'{rel} not found: save the project in the repository folder')
    for dat, rel in pairs:
        dat.par.file = rel
        dat.par.syncfile = True
    comp.par.reinitextensions.pulse()
    print(f'link_to_repo: {len(pairs)} DATs linked; code from {comp.ext.LtcGenExt._src}')
