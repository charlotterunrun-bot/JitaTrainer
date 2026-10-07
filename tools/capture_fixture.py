"""真实录音采集与离线检测工具。

用途（技术方案 §19 要求：合成音不能替代真实录音验证）：

  1. 从麦克风录一段真实吉他音，存成 WAV（可作为回归测试固件）；
  2. 立刻用项目自己的 YIN 实现离线分析，报告识别结果与音分偏差。

用法：

    python tools/capture_fixture.py --list                      # 列出设备
    python tools/capture_fixture.py --seconds 3 --out tests/fixtures/e2.wav
    python tools/capture_fixture.py --device 5 --seconds 2 --label "第6弦空弦"
    python tools/capture_fixture.py --analyze tests/fixtures/e2.wav   # 只分析已有文件

录音只保存在你指定的路径，不会自动上传任何地方。
"""

from __future__ import annotations

import argparse
import sys
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
for extra in (ROOT / ".vendor", ROOT / "src"):
    if extra.is_dir() and str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from jitatrainer import compat  # noqa: E402

compat.install()

from jitatrainer.core.audio.device import list_input_devices  # noqa: E402
from jitatrainer.core.audio.gate import frame_rms_db  # noqa: E402
from jitatrainer.core.audio.pitch_yin import PitchDetector  # noqa: E402
from jitatrainer.core.theory.notes import hz_to_midi, midi_name  # noqa: E402

SAMPLERATE = 48000
WINDOW = 2048
HOP = 480


def list_devices() -> int:
    devices = list_input_devices()
    if not devices:
        print("未找到输入设备")
        return 1
    print("可用输入设备：")
    for item in devices:
        mark = "★" if item.is_default else " "
        print(f"  {mark} [{item.index:3d}] {item.name}  ({item.max_input_channels}ch @ {int(item.default_samplerate)}Hz)")
    return 0


def record(seconds: float, device: int | None, out: Path, lead_in: float = 0.0) -> Path:
    import sounddevice as sd
    import time

    out.parent.mkdir(parents=True, exist_ok=True)
    if lead_in > 0:
        print(f"预备…… {lead_in:.0f} 秒后开始录音，请准备拨弦")
        for remaining in range(int(lead_in), 0, -1):
            print(f"  {remaining} …", flush=True)
            time.sleep(1.0)

    print(f"● 开始录音 {seconds:.0f}s —— 请依次拨响 6→5→4→3→2→1 弦（每根约 2 秒）", flush=True)
    data = sd.rec(
        int(seconds * SAMPLERATE),
        samplerate=SAMPLERATE,
        channels=1,
        dtype="float32",
        device=device,
    )
    sd.wait()
    print("■ 录音结束")
    samples = np.asarray(data)[:, 0]

    clipped = int(np.sum(np.abs(samples) >= 0.999))
    peak_db = frame_rms_db(samples[np.argmax(np.abs(samples)) : np.argmax(np.abs(samples)) + WINDOW]) if samples.size else -120.0
    print(f"采集完成：{samples.size} 帧，峰值附近 {peak_db:.1f} dB，削顶样本 {clipped}")

    with wave.open(str(out), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLERATE)
        handle.writeframes((np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes())
    print(f"已保存：{out}")
    return out


def load_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as handle:
        frames = handle.readframes(handle.getnframes())
        width = handle.getsampwidth()
        channels = handle.getnchannels()
    dtype = {2: "<i2", 4: "<i4"}.get(width)
    if dtype is None:
        raise SystemExit(f"不支持的位深：{width * 8} 位")
    data = np.frombuffer(frames, dtype=dtype).astype(np.float64)
    if width == 2:
        data /= 32768.0
    elif width == 4:
        data /= 2147483648.0
    if channels > 1:
        data = data.reshape(-1, channels)[:, 0]
    return data


def analyze(path: Path, samplerate: int = SAMPLERATE) -> int:
    samples = load_wav(path)
    detector = PitchDetector()

    readings: list[tuple[float, float, int]] = []
    for start in range(0, max(0, samples.size - WINDOW), HOP):
        frame = samples[start : start + WINDOW]
        if frame.size < WINDOW:
            break
        if frame_rms_db(frame) < -70.0:
            continue
        result = detector.detect(frame)
        if result.valid and result.pitch_class is not None:
            readings.append((start / samplerate, result.hz, result.pitch_class))

    if not readings:
        print(f"{path.name}：未检测到有效音高（可能是静音或噪声）")
        return 1

    counts: dict[int, int] = {}
    for _t, _hz, pc in readings:
        counts[pc] = counts.get(pc, 0) + 1
    dominant_pc, dominant_count = max(counts.items(), key=lambda kv: kv[1])
    dominant_hz = float(np.median([hz for _t, hz, pc in readings if pc == dominant_pc]))

    dominant_midi = hz_to_midi(dominant_hz)
    nearest_midi = round(dominant_midi)
    cents_offset = 100.0 * (dominant_midi - nearest_midi)

    print(f"\n{path.name} 分析结果")
    print(f"  有效帧 {len(readings)} / 总时长 {samples.size / samplerate:.2f}s")
    print(
        f"  主音：{midi_name(nearest_midi)}（音名序号 {dominant_pc}），"
        f"{dominant_count}/{len(readings)} 帧一致"
    )
    print(f"  中位频率 {dominant_hz:.2f} Hz，标准音 {440.0 * 2 ** ((nearest_midi - 69) / 12):.2f} Hz，"
          f"偏差 {cents_offset:+.1f} 音分")
    if len(counts) > 1:
        others = ", ".join(
            midi_name(round(hz_to_midi(hz)))
            for pc, hz in sorted(counts.items(), key=lambda kv: -kv[1])
            if counts[pc] >= 2
        )
        if others:
            print(f"  其它音名：{others}（可能是泛音、环境声或相邻弦共振）")
    return 0


def analyze_segments(samples: np.ndarray, samplerate: int = SAMPLERATE, min_frames: int = 6) -> list[dict]:
    """把一段录音切成"稳定的音"的序列，用于识别依次拨响的六根弦。

    门限自适应：取全曲电平的 20 分位数作为噪声地板，+12dB 作为门限，
    这样无论录音偏轻还是偏响都能正常工作。
    """
    detector = PitchDetector()

    levels: list[float] = []
    frames: list[tuple[float, float | None]] = []
    for start in range(0, max(0, samples.size - WINDOW), HOP):
        frame = samples[start : start + WINDOW]
        if frame.size < WINDOW:
            break
        level = frame_rms_db(frame)
        levels.append(level)
        frames.append((start / samplerate, level))

    floor = float(np.percentile(levels, 20)) if levels else -120.0
    gate = max(-70.0, min(-20.0, floor + 12.0))

    readings: list[tuple[float, float | None]] = []
    for t, _level in frames:
        start = int(t * samplerate)
        frame = samples[start : start + WINDOW]
        if frame_rms_db(frame) < gate:
            readings.append((t, None))
            continue
        result = detector.detect(frame)
        readings.append((t, float(result.hz) if result.valid else None))

    segments: list[dict] = []
    current: list[tuple[float, float]] = []
    current_pc: int | None = None
    for t, hz in readings:
        pc = round(hz_to_midi(hz)) % 12 if hz else None
        if pc is not None and pc == current_pc:
            current.append((t, hz))  # type: ignore[arg-type]
            continue
        if len(current) >= min_frames:
            segments.append(_summarize_segment(current))
        current = [(t, hz)] if pc is not None else []  # type: ignore[list-item]
        current_pc = pc
    if len(current) >= min_frames:
        segments.append(_summarize_segment(current))

    return [{"gate_db": round(gate, 1), "floor_db": round(floor, 1)}, *segments]


def _summarize_segment(items: list[tuple[float, float]]) -> dict:
    times = [t for t, _ in items]
    hz_values = [hz for _, hz in items]
    median_hz = float(np.median(hz_values))
    midi = hz_to_midi(median_hz)
    nearest = round(midi)
    return {
        "start": round(min(times), 2),
        "end": round(max(times) + HOP / SAMPLERATE, 2),
        "frames": len(items),
        "note": midi_name(nearest),
        "hz": round(median_hz, 2),
        "cents": round(100.0 * (midi - nearest), 1),
    }


def report_segments(path: Path) -> int:
    samples = load_wav(path)
    segments = analyze_segments(samples)
    if not segments:
        print(f"\n{path.name}：未检测到任何稳定音")
        return 1

    meta = segments[0]
    print(f"\n{path.name} 分段识别结果")
    print(f"  噪声地板 {meta['floor_db']} dB，门限 {meta['gate_db']} dB")
    print(f"  共识别到 {len(segments) - 1} 个稳定音：\n")
    print("  #  起始    持续    音名     频率(Hz)   音分偏差")
    print("  -- ----- ------- -------- --------- ----------")
    for index, seg in enumerate(segments[1:], start=1):
        duration = seg["end"] - seg["start"]
        print(
            f"  {index:2d} {seg['start']:5.2f}s {duration:6.2f}s {seg['note']:>8} "
            f"{seg['hz']:9.2f} {seg['cents']:+9.1f}"
        )

    notes = [seg["note"] for seg in segments[1:]]
    print(f"\n  音名序列：{' → '.join(notes)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="真实录音采集与离线检测")
    parser.add_argument("--list", action="store_true", help="列出输入设备")
    parser.add_argument("--device", type=int, default=None, help="设备序号")
    parser.add_argument("--seconds", type=float, default=2.0, help="录音时长（秒）")
    parser.add_argument("--out", type=Path, default=None, help="WAV 输出路径")
    parser.add_argument("--analyze", type=Path, default=None, help="只分析已有 WAV，不录音")
    parser.add_argument("--segments", action="store_true", help="分段识别（用于依次拨响六根弦）")
    parser.add_argument("--lead-in", type=float, default=0.0, help="录音前的预备倒计时（秒）")
    parser.add_argument("--label", default="", help="本次录音的说明（仅用于打印）")
    args = parser.parse_args()

    if args.list:
        return list_devices()

    if args.analyze is not None:
        if not args.analyze.is_file():
            raise SystemExit(f"文件不存在：{args.analyze}")
        return report_segments(args.analyze) if args.segments else analyze(args.analyze)

    out = args.out or (ROOT / "tests" / "fixtures" / "capture.wav")
    if args.label:
        print(f"本次录音：{args.label}")
    path = record(args.seconds, args.device, out, lead_in=args.lead_in)
    return report_segments(path) if args.segments else analyze(path)


if __name__ == "__main__":
    raise SystemExit(main())
