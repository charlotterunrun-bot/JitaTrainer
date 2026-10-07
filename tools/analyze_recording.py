"""录音分析：把一段吉他录音按弦归类，并给出低频响应诊断。

用法：

    python tools/analyze_recording.py 用户提供资料/录音1.m4a
    python tools/analyze_recording.py 录音.wav --order 6,5,4,3,2,1
    python tools/analyze_recording.py 录音.wav --order 1,2,3,4,5,6

支持 m4a/mp3/wav（非 WAV 会用 ffmpeg 转成 48kHz 单声道临时文件）。

输出三部分：
  1. 有声音的秒（时间轴）——即使识别不出音高也会列出，便于定位"哪根弦没录到"
  2. 分段识别并按弦归类——每根弦拨了几下、中位频率、与标准音的偏差
  3. 低频响应诊断——基频区与各次谐波的相对电平（判断麦克风低频能力）
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for extra in (ROOT / ".vendor", ROOT / "src", ROOT / "tools"):
    if extra.is_dir() and str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import numpy as np  # noqa: E402

from jitatrainer import compat  # noqa: E402

compat.install()

from capture_fixture import analyze_segments  # noqa: E402
from jitatrainer.core.audio.gate import frame_rms_db  # noqa: E402
from jitatrainer.core.audio.pitch_yin import PitchDetector, YinConfig  # noqa: E402
from jitatrainer.core.theory.notes import cents_between, hz_to_midi, midi_name, midi_to_hz  # noqa: E402

WINDOW = 2048
HOP = 480
NFFT = 32768

#: 弦号 → (标准音高 MIDI, 名称)
STRING_INFO = {
    6: (40, "第6弦 E2"),
    5: (45, "第5弦 A2"),
    4: (50, "第4弦 D3"),
    3: (55, "第3弦 G3"),
    2: (59, "第2弦 B3"),
    1: (64, "第1弦 E4"),
}

#: 各弦基频区与二次、三次谐波区（与弦号对应）
HARMONIC_BANDS = {
    6: [(70, 92), (150, 175), (235, 255)],
    5: [(100, 122), (212, 240), (320, 355)],
    4: [(133, 160), (280, 320), (425, 470)],
}


def to_wav(path: Path) -> tuple[Path, bool]:
    """非 WAV 用 ffmpeg 转成 48kHz 单声道 WAV。返回 (路径, 是否为临时文件)。"""
    if path.suffix.lower() == ".wav":
        return path, False
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise SystemExit("需要 ffmpeg 才能转换 m4a/mp3，请先安装或改用 WAV")
    out = Path(tempfile.mkdtemp(prefix="jita-rec-")) / (path.stem + ".wav")
    subprocess.run(
        [ffmpeg, "-y", "-v", "error", "-i", str(path), "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", str(out)],
        check=True,
    )
    return out, True


def load(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as handle:
        samplerate = handle.getframerate()
        samples = np.frombuffer(handle.readframes(handle.getnframes()), dtype="<i2").astype(np.float64) / 32768.0
    return samples, samplerate


def print_timeline(samples: np.ndarray, samplerate: int, detector: PitchDetector) -> None:
    print("[1] 有声音的秒（时间轴）")
    print("     秒   峰值(dB)  识别")
    for second in range(int(samples.size / samplerate)):
        chunk = samples[second * samplerate : (second + 1) * samplerate]
        if chunk.size < WINDOW:
            break
        peak = -200.0
        notes: dict[str, int] = {}
        for offset in range(0, chunk.size - WINDOW, HOP):
            frame = chunk[offset : offset + WINDOW]
            level = frame_rms_db(frame)
            peak = max(peak, level)
            if level < -70.0:
                continue
            result = detector.detect(frame)
            if result.valid:
                name = midi_name(round(hz_to_midi(result.hz)))
                notes[name] = notes.get(name, 0) + 1
        if peak <= -70.0:
            continue
        top = sorted(notes.items(), key=lambda kv: -kv[1])[:3]
        label = "  ".join(f"{n}×{c}" for n, c in top) or "（识别不出音高）"
        print(f"    {second:4d}  {peak:8.1f}  {label}")


def print_by_string(samples: np.ndarray, samplerate: int, order: list[int]) -> None:
    print("\n[2] 分段识别并按弦归类")
    segments = analyze_segments(samples, samplerate)[1:]
    expected = {n: midi_to_hz(STRING_INFO[n][0]) for n in order}
    grouped: dict[int, list[dict]] = {n: [] for n in order}

    for seg in segments:
        midi = hz_to_midi(seg["hz"])
        best, best_delta = None, 99.0
        for number in order:
            delta = abs(midi - STRING_INFO[number][0])
            if delta < best_delta:
                best, best_delta = number, delta
        if best is not None and best_delta <= 1.6:
            grouped[best].append(seg)

    print("     弦              拨弦  中位频率       音分偏差   结论")
    for number in order:
        name = STRING_INFO[number][1]
        segs = grouped[number]
        if not segs:
            print(f"     {name:14}  —      —             —        ❌ 未识别到")
            continue
        median_hz = float(np.median([s["hz"] for s in segs]))
        cents = cents_between(median_hz, expected[number])
        print(f"     {name:14} {len(segs):3d}   {median_hz:8.2f}Hz {cents:+9.1f}   ✅ 识别正常")


def print_low_bands(samples: np.ndarray, samplerate: int, order: list[int]) -> None:
    print("\n[3] 低频响应诊断（相对电平，以该段最强峰为 0dB）")
    duration = samples.size / samplerate
    # 按顺序把录音均分成 len(order) 段，逐段做频谱诊断
    span = duration / max(1, len(order))
    for index, number in enumerate(order):
        bands = HARMONIC_BANDS.get(number)
        if bands is None:
            continue
        start, end = index * span, (index + 1) * span
        chunk = samples[int(start * samplerate) : int(end * samplerate)]
        if chunk.size < 4096:
            continue
        spec = np.abs(np.fft.rfft(chunk * np.hanning(chunk.size), NFFT))
        freqs = np.fft.rfftfreq(NFFT, 1 / samplerate)
        overall = float(np.max(spec)) or 1e-12

        levels: list[float] = []
        for label, (lo, hi) in zip(("基频", "2次谐波", "3次谐波"), bands):
            mask = (freqs >= lo) & (freqs <= hi)
            if not mask.any():
                levels.append(-200.0)
                continue
            peak = float(np.max(spec[mask]))
            levels.append(20 * np.log10(max(peak, 1e-12) / overall))
        del label  # 仅用于可读性

        print(
            f"     {STRING_INFO[number][1]:14} 段 {start:5.1f}-{end:5.1f}s  "
            f"基频 {levels[0]:+6.1f}dB   2次 {levels[1]:+6.1f}dB   3次 {levels[2]:+6.1f}dB"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="吉他录音逐弦分析")
    parser.add_argument("input", type=Path, help="录音文件（wav/m4a/mp3）")
    parser.add_argument("--order", default="6,5,4,3,2,1", help="弹奏的弦号顺序，逗号分隔")
    parser.add_argument("--no-timeline", action="store_true", help="跳过逐秒时间轴")
    args = parser.parse_args()

    if not args.input.is_file():
        raise SystemExit(f"文件不存在：{args.input}")

    order = [int(part) for part in args.order.replace(" ", "").split(",") if part]
    for number in order:
        if number not in STRING_INFO:
            raise SystemExit(f"弦号必须是 1-6，收到 {number}")

    wav_path, is_temp = to_wav(args.input)
    samples, samplerate = load(wav_path)
    peak_db = 20 * np.log10(max(float(np.max(np.abs(samples))), 1e-12))

    print("=" * 78)
    print(f"{args.input.name}：{samples.size / samplerate:.1f}s @ {samplerate}Hz，峰值 {peak_db:.1f} dBFS")
    print(f"弹奏顺序：{' → '.join(str(n) for n in order)}")
    print("=" * 78)

    detector = PitchDetector(YinConfig(samplerate=samplerate, window=WINDOW))
    if not args.no_timeline:
        print_timeline(samples, samplerate, detector)
    print_by_string(samples, samplerate, order)
    print_low_bands(samples, samplerate, order)

    if is_temp:
        wav_path.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
