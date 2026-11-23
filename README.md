# LTC File Gen

A TouchDesigner component that renders timecode movie files for media servers. Each file has an LTC audio track, a visual timecode readout and a QuickTime timecode track.

## Features

- **Rates:** 23.976, 24, 25, 29.97 (DF or NDF), 30, 48, 50, 59.94 (DF or NDF) and 60 fps. Above 30 fps the LTC runs at half rate in frame pairs.
- **Timing:** start timecode, duration and pre-roll. Pre-roll can cross midnight.
- **Batches:** a list of files with their own settings, rendered in one go.
- **Codecs:** HAP, HAP Q, NotchLC, ProRes 422 / 422 HQ, H.264 and H.265 in `.mov`.
- **Size:** 1920x1080 by default, presets up to 3840x2160, or a custom size.
- **Picture:** a timecode readout. Optionally add a second count above 30 fps (LTC or video frames), a countdown, a sync flash, a frame marker and an info caption.
- **LTC audio:** SMPTE ST 12-1. Defaults are 48 kHz, 24-bit, both channels and −12 dBFS. Rate, depth, channels, level, rise time and user bits are adjustable.
- **Checked:** every file is read back and checked for frame count, rate, audio length and start timecode.

## Requirements

- **TouchDesigner 2025.33230 or later.** Tested on macOS only. The Non-Commercial licence limits output to 1280x1280. H.264/H.265 export needs a Commercial or Pro licence.
- **ffmpeg**, which adds the LTC audio. Click **?** in the panel and choose **Install Now**, or use an existing ffmpeg (Homebrew, MacPorts, winget, Chocolatey, Scoop or a custom path). The **?** button is blue when ffmpeg is ready and pulses red when it is missing.

## Getting started

Download from [Releases](https://github.com/jpkelly/TDXLTCfileGen/releases):

- **`LTCfileGen-<version>.toe`:** open it, and TouchDesigner shows the panel in its own window (Perform mode). Press Esc for the network, F1 to return.
- **`LTCfileGen-<version>.tox`:** drag it into a network of your own project, then right-click it and choose **View** to open the panel.

Set the rate, start, duration, codec and output folder, then click **Render**. The line under the preview shows the file it will write. For a batch, set up each file and click **Add current**, then **Render batch**. **More settings…** opens the picture options (readouts, countdown, colours), the LTC audio settings and the ffmpeg setup.

The component carries its own code. On first use it writes it to a versioned folder in the user data folder (`~/Library/Application Support/LTCfileGen/engine` on macOS, `%LOCALAPPDATA%\LTCfileGen\engine` on Windows).

## Engines

The **Engine** menu picks how files are made:

| Engine | How | 1 h of 59.94 fps HAP (Apple M4 Max) |
|---|---|---|
| ffmpeg fast | numpy frames piped into ffmpeg, in a background process | about 3 min |
| TouchDesigner | Movie File Out, one frame per TouchDesigner frame | about 30 min |

**Auto** (the default) uses ffmpeg fast when it can. Before every fast render, it checks that its frames match TouchDesigner's. NotchLC always uses TouchDesigner, because ffmpeg has no NotchLC encoder. If the fast engine fails, Auto renders in TouchDesigner instead.

The TouchDesigner engine runs as fast as the display refreshes, for example about 120 frames/s on a 120 Hz screen. While it renders, the project runs at the fastest display's refresh rate, then returns to its own FPS. Anything else animating in the same project runs at that rate meanwhile, so render show content from the standalone `.toe`.

## Development

The timecode maths, LTC encoder, picture and ffmpeg code are plain Python in `src/ltcfilegen`. It is tested without TouchDesigner.

```sh
# macOS: a venv on TouchDesigner's own Python (it has numpy)
/Applications/TouchDesigner.app/Contents/Frameworks/Python.framework/Versions/3.11/bin/python3.11 \
  -m venv --system-site-packages .venv
.venv/bin/pip install pytest
.venv/bin/python -m pytest -m "not slow"
```

Some tests need ffmpeg, or [ltc-tools](https://github.com/x42/ltc-tools) (`ltcgen`, `ltcdump`) as an independent decoder. They are skipped without them.

To work on the component, save a TouchDesigner project in the repository folder, drag in `td/LTCfileGen.tox` and link its code to the repository files. In the textport:

```python
exec(open(project.folder + '/tools/td_release.py', encoding='utf-8').read())
link_to_repo()
```

Edits to `td/` and `src/ltcfilegen/` then take effect in TouchDesigner. The panel is built by `tools/td_build_ui.py` (`build_ui()`, run the same way) from TouchDesigner's Basic Widgets, each bound to one of the component's parameters.

`tools/av_check.py` checks rendered files frame by frame. It reads a debug barcode from the picture (**More settings… → Picture → Debug Barcode Strip**) and compares it with the decoded LTC.

| Path | Contents |
|---|---|
| `src/ltcfilegen/` | timecode, job planning, LTC, picture, ffmpeg and fast-render worker |
| `td/LtcGenExt.py` | the component's extension (render loop, panel, batch) |
| `td/glsl/picture.glsl` | the picture shader |
| `td/LTCfileGen.tox` | the component, self-contained |
| `tests/` | tests |
| `tools/` | render checker, panel builder and release tools |

### Releasing

1. In the development project, run `release_tox()` (from `tools/td_release.py`, like `link_to_repo()` above). It writes `td/LTCfileGen.tox` with the code baked in and every setting at its default.
2. Run `python3 tools/release_toe.py`. It puts the component in TouchDesigner's own empty project and writes `dist/LTCfileGen-<version>.toe`, which opens straight to the panel, plus a copy of the `.tox`.

## Status

Early release. Tested on macOS with TouchDesigner 2025.33230. Windows is untested.

## Licence

[MIT](LICENSE)
