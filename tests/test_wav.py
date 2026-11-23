"""LTC as a WAV file (File type: WAV only / Movie + WAV)."""

import wave

import pytest

from ltcfilegen.job import JobSpec, Plan
from ltcfilegen.ltc import AudioOptions, LtcEncoder, check_wav_size, pcm_to_wav


def _enc(**audio):
    plan = Plan(JobSpec(rate="29.97", drop_frame=True, start="00:59:58;00", duration="00:00:05;00"))
    return LtcEncoder(plan, AudioOptions(**audio))


@pytest.mark.parametrize("audio", [{}, {"bit_depth": 16}, {"layout": "mono"}, {"sample_rate": 44100}])
def test_wav_holds_exactly_the_movie_track(tmp_path, audio):
    enc = _enc(**audio)
    pcm = enc.write_pcm(str(tmp_path / "a.pcm"))
    assert enc.write_wav(str(tmp_path / "a.wav"))
    with wave.open(str(tmp_path / "a.wav")) as w:
        assert w.getnframes() == enc.total
        assert w.getframerate() == enc.sr
        assert w.getnchannels() == enc.opts.channels
        assert w.getsampwidth() == enc.opts.bit_depth // 8
        data = w.readframes(w.getnframes())
    assert data == open(pcm["path"], "rb").read()


def test_pcm_to_wav_matches_write_wav(tmp_path):
    enc = _enc()
    pcm = enc.write_pcm(str(tmp_path / "a.pcm"))
    assert pcm_to_wav(pcm["path"], str(tmp_path / "b.wav"), enc.opts, chunk=1000)
    enc.write_wav(str(tmp_path / "c.wav"))
    assert open(tmp_path / "b.wav", "rb").read() == open(tmp_path / "c.wav", "rb").read()


def test_cancel_stops_writing(tmp_path):
    enc = _enc()
    assert enc.write_wav(str(tmp_path / "a.wav"), chunk=1000, cancelled=lambda: True) is False


def test_wav_size_limit():
    opts = AudioOptions()                          # 48 kHz, 24-bit, stereo: 288,000 bytes/s
    check_wav_size(48000 * 3600 * 4, opts)          # 4 h fits
    with pytest.raises(ValueError, match="at most 4.1 h"):
        check_wav_size(48000 * 3600 * 5, opts)


def test_wavwrite_worker_matches_write_wav(tmp_path):
    import dataclasses, json, os, subprocess, sys
    enc = _enc()
    enc.write_wav(str(tmp_path / "ref.wav"))
    job = {"spec": dataclasses.asdict(enc.plan.spec), "audio": dataclasses.asdict(enc.opts),
           "out": str(tmp_path / "w.wav"), "status": str(tmp_path / "st.json")}
    (tmp_path / "job.json").write_text(json.dumps(job))
    env = dict(os.environ, PYTHONPATH=os.path.join(os.path.dirname(__file__), "..", "src"))
    rc = subprocess.run([sys.executable, "-m", "ltcfilegen.wavwrite", str(tmp_path / "job.json")], env=env).returncode
    assert rc == 0 and json.loads((tmp_path / "st.json").read_text())["phase"] == "done"
    assert (tmp_path / "w.wav").read_bytes() == (tmp_path / "ref.wav").read_bytes()
