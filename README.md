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

1. Clone or download this repository.
2. In TouchDesigner, save a new project in the repository folder. The component loads its code from `td/` and `src/` next to the project.
3. Drag `td/LTCfileGen.tox` into the project and open its panel.
4. Set the rate, start, duration, codec and output folder, then click **Render**.

For a batch, set up each file and click **Add current**, then **Render batch**.

## Engines

The **Engine** menu on the Video page picks how files are made:

| Engine | How | 1 h of 59.94 fps HAP (Apple M4 Max) |
|---|---|---|
| ffmpeg fast | numpy frames piped into ffmpeg, in a background process | about 3 min |
| TouchDesigner | Movie File Out, one frame per TouchDesigner frame | about 30 min |

**Auto** (the default) uses ffmpeg fast when it can. Before every fast render, it checks that its frames match TouchDesigner's. NotchLC always uses TouchDesigner, because ffmpeg has no NotchLC encoder. If the fast engine fails, Auto renders in TouchDesigner instead.

The TouchDesigner engine runs as fast as the display refreshes: about 120 frames/s on a 120 Hz screen.

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

`tools/av_check.py` checks rendered files frame by frame. It reads a debug barcode from the picture (Picture page, **Debug Barcode Strip**) and compares it with the decoded LTC.

| Path | Contents |
|---|---|
| `src/ltcfilegen/` | timecode, job planning, LTC, picture, ffmpeg and fast-render worker |
| `td/LtcGenExt.py` | the component's extension (render loop, panel, batch) |
| `td/glsl/picture.glsl` | the picture shader |
| `td/LTCfileGen.tox` | the component |
| `tests/`, `tools/` | tests and the render checker |

## Status

Early release. Tested on macOS with TouchDesigner 2025.33230. Windows is untested.

## Licence

[MIT](LICENSE)
