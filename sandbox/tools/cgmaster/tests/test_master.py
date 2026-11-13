from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from cgmaster import master
from cgmaster.errors import SYNTH_FAILED, CgmasterError

RATE = 44100


def sine(seconds: float = 4.0, frequency: float = 440.0, amplitude: float = 0.05) -> np.ndarray:
    t = np.arange(int(RATE * seconds), dtype=np.float32) / RATE
    mono = amplitude * np.sin(2 * np.pi * frequency * t)
    return np.column_stack((mono, mono)).astype(np.float32)


def setup_stems(tmp_path: Path, audio: np.ndarray | None = None) -> tuple[Path, Path, Path]:
    stems = tmp_path / "stems"
    stems.mkdir()
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "version": 1,
                "parts": [
                    {
                        "id": "P1",
                        "name": "sine",
                        "family": "woodwinds",
                        "engine_hint": "muse",
                        "mix": {
                            "gain_db": -6.0,
                            "pan": 0.0,
                            "reverb_send": 0.0,
                            "echo_send": 0.0,
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    sf.write(stems / "P1.wav", sine() if audio is None else audio, RATE, subtype="FLOAT")
    output = tmp_path / "out.wav"
    return plan, stems, output


@pytest.mark.parametrize("gain", [0.0, 6.0, -6.0])
def test_loudness_true_peak_and_gain(tmp_path: Path, gain: float) -> None:
    plan, stems, output = setup_stems(tmp_path)
    report = master(plan, stems, output, fmt="wav", gain=gain)
    assert report["lufs"] == pytest.approx(-14 + gain, abs=0.1)
    assert float(str(report["true_peak_dbtp"])) <= -1.5
    assert report["clipped"] is False
    assert report["tracks"] == [
        {"id": "P1", "rms_dbfs": pytest.approx(-29.03, abs=0.02), "gain_db": -6.0}
    ]


def test_gain_clamps_to_twelve_db(tmp_path: Path) -> None:
    plan, stems, output = setup_stems(tmp_path)
    high = master(plan, stems, output, fmt="wav", gain=100)
    # 持续正弦的峰值/响度比使 -2 LUFS 在 -1.5 dBTP 内不可达；增益仍截到 +12 dB。
    unlimited = master(plan, stems, output.with_name("unlimited.wav"), fmt="wav", gain=12)
    assert high["lufs"] == pytest.approx(unlimited["lufs"], abs=0.1)
    assert float(str(high["true_peak_dbtp"])) <= -1.5
    low = master(plan, stems, output, fmt="wav", gain=-100)
    assert low["lufs"] == pytest.approx(-26, abs=0.1)


def test_transient_peak_cap(tmp_path: Path) -> None:
    audio = sine(amplitude=0.2)
    audio[1000, 0] = 4.0
    plan, stems, output = setup_stems(tmp_path, audio)
    report = master(plan, stems, output, fmt="wav", gain=12)
    assert float(str(report["true_peak_dbtp"])) <= -1.5
    assert report["clipped"] is False


def test_trims_silence_and_fades_tail(tmp_path: Path) -> None:
    body = sine(seconds=2.0)
    padded = np.pad(body, ((RATE, 2 * RATE), (0, 0)))
    plan, stems, output = setup_stems(tmp_path, padded)
    report = master(plan, stems, output, fmt="wav")
    written, _ = sf.read(output, always_2d=True)
    duration = float(str(report["duration_s"]))
    assert 1.95 <= duration <= 2.05
    assert np.max(np.abs(written[:100])) > 0.001
    assert written[-1, 0] == 0.0


def test_rejects_silent_stems(tmp_path: Path) -> None:
    plan, stems, output = setup_stems(tmp_path, np.zeros((RATE, 2), dtype=np.float32))
    with pytest.raises(CgmasterError) as error:
        master(plan, stems, output, fmt="wav")
    assert error.value.code == SYNTH_FAILED
    assert "静音" in error.value.message
    assert not output.exists()


@pytest.mark.parametrize("fmt", ["wav", "flac", "mp3"])
def test_writes_every_format(tmp_path: Path, fmt: str) -> None:
    plan, stems, _ = setup_stems(tmp_path)
    output = tmp_path / f"music.{fmt}"
    report = master(plan, stems, output, fmt=fmt)
    assert output.stat().st_size > 1000
    assert report["fmt"] == fmt
    assert report["path"] == str(output.absolute())
    with sf.SoundFile(output) as stream:
        assert stream.samplerate == RATE
        assert stream.channels == 2


def test_pan_sends_and_balance_report(tmp_path: Path) -> None:
    plan, stems, output = setup_stems(tmp_path)
    data = json.loads(plan.read_text())
    data["parts"][0]["mix"].update(pan=-1.0, reverb_send=30, echo_send=0.2)
    plan.write_text(json.dumps(data))
    report = master(plan, stems, output, fmt="wav")
    balance = report["balance"]
    reverb = report["reverb"]
    assert isinstance(balance, list) and balance[0]["pan"] == -1.0
    assert isinstance(reverb, dict) and isinstance(reverb["channels"], list)
    assert reverb["channels"][0]["reverb_send"] == pytest.approx(30 / 127)
    assert reverb["channels"][0]["echo_send"] == 0.2


def test_cli_json_and_error(tmp_path: Path) -> None:
    plan, stems, output = setup_stems(tmp_path)
    env = {"PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    result = subprocess.run(
        [sys.executable, "-m", "cgmaster.cli", "mix", str(plan), "--stems", str(stems),
         "--fmt", "wav", "-o", str(output)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["path"] == str(output.absolute())

    result = subprocess.run(
        [sys.executable, "-m", "cgmaster.cli", "mix", str(tmp_path / "missing.json"),
         "--stems", str(stems)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 1
    assert json.loads(result.stderr)["error"] == "bad_input"
