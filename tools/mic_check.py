"""麦克风实时电平与音高检测（自检用）。

用途：在正式录音/练习之前，先确认"这个设备到底能不能拾到吉他的声音"。
它会实时显示电平条与识别到的音名，并且统计有多少比例的帧真正有信号。

用法：

    python tools/mic_check.py --list                  # 列出设备
    python tools/mic_check.py --device 2 --seconds 20 # 用指定设备测 20 秒
    python tools/mic_check.py --seconds 20            # 用 Windows 默认录音设备

测的时候正常拨弦即可。结束时给出结论：设备是否可用。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for extra in (ROOT / ".vendor", ROOT / "src"):
    if extra.is_dir() and str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import numpy as np  # noqa: E402

from jitatrainer import compat  # noqa: E402

compat.install()

from jitatrainer.core.audio.device import list_input_devices  # noqa: E402
from jitatrainer.core.audio.gate import frame_rms_db  # noqa: E402
from jitatrainer.core.audio.pitch_yin import PitchDetector, YinConfig  # noqa: E402
from jitatrainer.core.theory.notes import hz_to_midi, midi_name  # noqa: E402

WINDOW = 2048
BLOCK = 512
BAR_WIDTH = 30


def bar(level_db: float, gate_db: float = -55.0) -> str:
    """把 dBFS 映射成一条可见的电平条。"""
    low, high = -70.0, -6.0
    ratio = 0.0 if level_db <= low else min(1.0, (level_db - low) / (high - low))
    filled = int(ratio * BAR_WIDTH)
    mark = "█" * filled + "·" * (BAR_WIDTH - filled)
    flag = " ← 有信号" if level_db > gate_db else ""
    return f"|{mark}|{flag}"


def main() -> int:
    parser = argparse.ArgumentParser(description="麦克风实时电平检测")
    parser.add_argument("--list", action="store_true", help="列出输入设备")
    parser.add_argument("--device", type=int, default=None, help="设备序号（默认用系统默认设备）")
    parser.add_argument("--seconds", type=float, default=15.0, help="检测时长")
    parser.add_argument("--samplerate", type=int, default=None, help="采样率（默认取设备默认值）")
    args = parser.parse_args()

    if args.list:
        devices = list_input_devices()
        if not devices:
            print("未找到输入设备")
            return 1
        print("可用输入设备：")
        for item in devices:
            mark = "★" if item.is_default else " "
            print(
                f"  {mark} [{item.index:3d}] {item.name[:44]:44} "
                f"{item.max_input_channels}ch @ {int(item.default_samplerate)}Hz"
            )
        return 0

    import sounddevice as sd

    info = sd.query_devices(args.device) if args.device is not None else sd.query_devices(kind="input")
    samplerate = args.samplerate or int(info["default_samplerate"])
    name = (
        info["name"]
        if args.device is not None
        else sd.query_devices(kind="input")["name"]
    )
    print(f"设备：{name}")
    print(f"采样率：{samplerate}Hz    时长：{args.seconds:.0f}s")
    print("请正常拨弦（不用按品），观察下面的电平条。\n")

    detector = PitchDetector(YinConfig(samplerate=samplerate, window=WINDOW))
    rolling = np.zeros(WINDOW, dtype=np.float32)

    total_frames = 0
    signal_frames = 0
    peak_db = -200.0
    note_counts: dict[str, int] = {}
    started = time.monotonic()

    with sd.InputStream(
        device=args.device, samplerate=samplerate, channels=1, dtype="float32", blocksize=BLOCK
    ) as stream:
        last_print = 0.0
        while time.monotonic() - started < args.seconds:
            block, _ = stream.read(BLOCK)
            data = np.asarray(block, dtype=np.float32)[:, 0]
            rolling = np.roll(rolling, -len(data))
            rolling[-len(data) :] = data

            level = frame_rms_db(rolling)
            peak_db = max(peak_db, level)
            total_frames += 1
            note = ""
            if level > -55.0:
                signal_frames += 1
                result = detector.detect(rolling.astype(np.float64))
                if result.valid:
                    note = f"{midi_name(round(hz_to_midi(result.hz)))}  {result.hz:7.1f}Hz"
                    note_counts[note.split()[0]] = note_counts.get(note.split()[0], 0) + 1

            now = time.monotonic() - started
            if now - last_print >= 0.25:
                last_print = now
                print(f"  {now:5.1f}s {level:7.1f}dB {bar(level)}  {note}", flush=True)

    elapsed = time.monotonic() - started
    ratio = signal_frames / max(1, total_frames)
    print(f"\n结果：")
    print(f"  采样帧数 {total_frames}，其中高于 −55dB 的 {signal_frames} 帧（{ratio * 100:.1f}%）")
    print(f"  期间最高电平 {peak_db:.1f} dBFS")
    if note_counts:
        top = sorted(note_counts.items(), key=lambda kv: -kv[1])[:5]
        print("  识别到的音：" + "  ".join(f"{name}×{count}" for name, count in top))

    print()
    if ratio > 0.3:
        print("  ✅ 该设备可以稳定拾音，适合练习与录音。")
        return 0
    if ratio > 0.05:
        print("  ⚠ 偶尔能拾到声音，但不稳定。检查麦克风距离、增益，或换一个设备。")
        return 2
    print("  ❌ 几乎拾不到声音。该设备不适合使用——请换用其他麦克风。")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
