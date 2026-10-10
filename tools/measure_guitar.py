"""测量吉他并生成配置文件。

用法::

    python tools\\measure_guitar.py 用户提供资料\\录音3.m4a --name "我的吉他"
    python tools\\measure_guitar.py 录音.wav --tuning standard --order 6,5,4,3,2,1

它会：

1. 用**程序运行时同一套分析链路**（两级分析窗口 + 谐波校验）逐次拨弦检测；
2. 把每次拨弦按"最接近哪根空弦"归类（用配置档案的预期音高，而不是靠频谱猜八度）；
3. 输出每根弦的实测频率、音分偏差、电平；
4. **对照预期给出提示**（走音、漏测、电平失衡、疑似换了调弦）；
5. 写出配置文件 JSON，供程序运行时加载。

配置文件的用途：程序按它选择检测策略、消解八度歧义、校验你的琴是否与配置相符。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src")]

import numpy as np  # noqa: E402

from jitatrainer import compat  # noqa: E402

compat.install()

from jitatrainer.core.audio.analyzer import AnalyzerConfig, FrameAnalyzer  # noqa: E402
from jitatrainer.core.audio.gate import GateConfig, frame_rms_db  # noqa: E402
from jitatrainer.core.instrument import (  # noqa: E402
    STANDARD_TUNING,
    TUNING_PRESETS,
    DetectionSettings,
    ProfileWarning,
    StringMeasurement,
    check_measurements,
    profile_from_measurements,
    resolve_hz,
    save_profile,
    standard_profile,
    summarise,
)
from jitatrainer.core.theory.notes import cents_between, midi_to_hz  # noqa: E402

#: 判定"这根弦调准了"的容差（音分）
IN_TUNE_CENTS = 12.0
#: 切分拨弦时的静音门限（dB）
SILENCE_DB = -45.0


def to_wav(path: Path) -> tuple[Path, bool]:
    """非 WAV 用 ffmpeg 转成 48kHz 单声道 WAV。"""
    if path.suffix.lower() == ".wav":
        return path, False
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise SystemExit("需要 ffmpeg 才能转换 m4a/mp3，请先安装或改用 WAV")
    out_dir = ROOT / ".tmp" / "measure"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / (path.stem + ".wav")
    subprocess.run(
        [ffmpeg, "-y", "-v", "error", "-i", str(path), "-ac", "1", "-ar", "48000",
         "-c:a", "pcm_s16le", str(out)],
        check=True,
    )
    return out, True


def load_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path)) as handle:
        rate = handle.getframerate()
        data = np.frombuffer(handle.readframes(handle.getnframes()), dtype=np.int16)
    return data.astype(np.float32) / 32768.0, rate


def split_plucks(samples: np.ndarray, rate: int, silence_db: float = SILENCE_DB):
    """按静音切出每次拨弦的样本区间。"""
    frame = int(rate * 0.02)
    spans: list[tuple[int, int]] = []
    active_from: int | None = None
    quiet = 0
    for start in range(0, len(samples) - frame, frame):
        level = frame_rms_db(samples[start : start + frame])
        if level > silence_db:
            if active_from is None:
                active_from = start
            quiet = 0
        elif active_from is not None:
            quiet += 1
            if quiet >= 8:  # 约 160ms 静音算结束
                spans.append((active_from, start))
                active_from, quiet = None, 0
    if active_from is not None:
        spans.append((active_from, len(samples)))
    return spans


def analyse_pluck(analyzer: FrameAnalyzer, chunk: np.ndarray, config: AnalyzerConfig, rate: int):
    """对一次拨弦做检测，返回置信度最高的那个事件。"""
    best = None
    need = config.required_samples
    if len(chunk) < need:
        chunk = np.pad(chunk, (0, need - len(chunk)))
    for offset in range(0, len(chunk) - need + 1, config.hop):
        event = analyzer.process(chunk[offset : offset + need], offset / rate)
        if event is not None and event.valid:
            if best is None or event.confidence > best.confidence:
                best = event
    return best


def main() -> int:
    parser = argparse.ArgumentParser(description="测量吉他并生成配置文件")
    parser.add_argument("input", help="录音文件（wav/m4a/mp3）")
    parser.add_argument("--name", default="我的吉他", help="配置档案名称")
    parser.add_argument(
        "--tuning",
        default="standard",
        choices=sorted(TUNING_PRESETS),
        help="预期调弦（默认标准调弦）",
    )
    parser.add_argument("--order", default="", help="弹奏顺序（逗号分隔），默认 6,5,4,3,2,1")
    parser.add_argument("--max-fret", type=int, default=12, help="最高品（用于可弹音集）")
    parser.add_argument(
        "--harmonic-mode",
        default=None,
        choices=("off", "auto", "presence"),
        help="谐波校验模式；默认按测量结果自动判断",
    )
    parser.add_argument("--out", default="", help="输出路径（默认 data/instruments/<id>.json）")
    parser.add_argument("--no-save", action="store_true", help="只打印结果，不写文件")
    args = parser.parse_args()

    source = Path(args.input)
    if not source.is_file():
        raise SystemExit(f"找不到录音文件：{source}")

    wav_path, temporary = to_wav(source)
    samples, rate = load_wav(wav_path)
    print(f"=== {source.name}：{len(samples) / rate:.1f}s @ {rate}Hz ===")

    tuning = TUNING_PRESETS[args.tuning]
    order = [int(item) for item in args.order.split(",") if item.strip()] or [
        number for number, _ in tuning
    ]

    base = standard_profile(max_fret=args.max_fret)
    config = AnalyzerConfig(
        samplerate=rate,
        low_enhance=base.detection.low_enhance,
        low_band_hz=base.detection.low_band_hz,
        harmonic_mode=args.harmonic_mode or base.detection.harmonic_mode,
    )
    analyzer = FrameAnalyzer(config, gate=GateConfig(noise_floor_db=-90.0, offset_db=5.0))

    spans = split_plucks(samples, rate)
    print(f"切分出 {len(spans)} 次拨弦\n")

    targets = dict(tuning)
    buckets: dict[int, list] = {number: [] for number, _ in tuning}
    for start, end in spans:
        chunk = samples[start:end]
        if len(chunk) < int(rate * 0.05):
            continue
        event = analyse_pluck(analyzer, chunk, config, rate)
        if event is None:
            continue
        # 按"最接近哪根空弦"归类：用配置里的预期音高消解八度，不靠频谱猜
        best_number, best_cents = None, float("inf")
        for number, midi in tuning:
            resolution = resolve_hz(event.hz, base, expect_midi=midi)
            if resolution.status == "unexplained":
                continue
            cents = abs(cents_between(resolution.hz, midi_to_hz(midi)))
            if cents < best_cents:
                best_number, best_cents = number, cents
        if best_number is None:
            print(
                f"  {start / rate:6.2f}s  {event.hz:8.2f}Hz  "
                f"（对不上任何一根弦，已忽略：可能不是吉他声）"
            )
            continue
        buckets[best_number].append(event)
        print(
            f"  {start / rate:6.2f}s  {event.hz:8.2f}Hz → 第{best_number}弦  "
            f"偏差 {cents_between(event.hz, midi_to_hz(targets[best_number])):+6.1f} 音分  "
            f"置信 {event.confidence:.3f}"
        )

    measurements: list[StringMeasurement] = []
    print("\n按弦汇总")
    print("  弦    次数   中位频率     音分偏差   电平(dB)   结论")
    for number, midi in tuning:
        events = buckets.get(number) or []
        if not events:
            print(f"  第{number}弦   0        —           —         —      ⚠ 未测到")
            continue
        hz = float(np.median([event.hz for event in events]))
        level = float(np.median([event.rms_db for event in events]))
        confidence = float(np.median([event.confidence for event in events]))
        cents = cents_between(hz, midi_to_hz(midi))
        verdict = "✅ 准" if abs(cents) <= IN_TUNE_CENTS else (
            "⚠ 偏高" if cents > 0 else "⚠ 偏低"
        )
        if abs(cents) > 40:
            verdict = "❌ 偏差过大"
        print(
            f"  第{number}弦 {len(events):4d}   {hz:8.2f}Hz   {cents:+7.1f}   "
            f"{level:7.1f}   {verdict}"
        )
        measurements.append(
            StringMeasurement(number=number, hz=hz, level_db=level, confidence=confidence)
        )

    # 谐波模式：如果低频弦在默认模式下读数明显不对，提示可用 presence
    mode = args.harmonic_mode
    mode_note = ""
    if mode is None:
        mode = base.detection.harmonic_mode
        low_strings = [m for m in measurements if m.number >= 5]
        suspicious = [
            m
            for m in low_strings
            if abs(cents_between(m.hz, midi_to_hz(targets[m.number]))) > 100
        ]
        if suspicious:
            mode = "presence"
            mode_note = (
                "（检测到低音弦读数明显偏离，已启用 presence 谐波校验；"
                "如果提示不准确请改用 --harmonic-mode auto）"
            )

    profile = profile_from_measurements(
        measurements,
        name=args.name,
        tuning=tuning,
        detection=DetectionSettings(harmonic_mode=mode, low_enhance=True),
        max_fret=args.max_fret,
        notes=f"由 {source.name} 实测生成{f'，{mode_note}' if mode_note else ''}。",
    )

    warnings: list[ProfileWarning] = check_measurements(measurements, profile)
    print("\n与配置的比对提示")
    if not warnings:
        print("  ✅ 没有发现问题")
    for warning in warnings:
        icon = {"info": "ℹ", "warn": "⚠", "error": "❌"}[warning.severity]
        print(f"  {icon} {warning.message}")
        if warning.advice:
            print(f"      → {warning.advice}")

    print(f"\n配置档案：{summarise(profile)}")
    print(f"  谐波校验模式：{profile.detection.harmonic_mode} {mode_note}")

    if not args.no_save:
        target = Path(args.out) if args.out else ROOT / "data" / "instruments" / f"{profile.id}.json"
        save_profile(profile, target)
        print(f"  已写入：{target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
