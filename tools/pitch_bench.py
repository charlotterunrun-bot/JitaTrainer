"""YIN 音高检测算法原型与基准测试（M0 技术方案的量化依据）。

这个脚本回答三个问题：
  1. 自实现的 YIN 在吉他音域（E2 82.41Hz ~ D6 1174Hz）上准不准？
  2. 低音 E2 需要多长的分析窗口？端到端延迟预算是多少？
  3. 加性噪声到什么程度会开始误判？谐波校验能否压住八度误判？

用法：
    python tools/pitch_bench.py            # 打印 Markdown 表格（可直接贴进技术方案）
    python tools/pitch_bench.py --json     # 输出机器可读结果

设计要点（与需求文档 §4.2 / FR-538 对应）：
  - 纯 numpy 实现 YIN，不依赖 aubio / librosa，控制体积与许可风险
  - 差分函数用 FFT 自相关计算，保证实时性
  - 增加谐波校验：若 f0/2 处能量足够强，判定为八度误判并修正
"""

from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import dataclass, asdict

import numpy as np

# --------------------------------------------------------------------------
# 音高工具
# --------------------------------------------------------------------------
A4_HZ = 440.0
A4_MIDI = 69
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def midi_to_hz(midi: float) -> float:
    return A4_HZ * 2.0 ** ((midi - A4_MIDI) / 12.0)


def hz_to_midi(hz: float) -> float:
    return A4_MIDI + 12.0 * math.log2(hz / A4_HZ)


def hz_to_name(hz: float) -> str:
    midi = round(hz_to_midi(hz))
    return f"{NOTE_NAMES[midi % 12]}{midi // 12 - 1}"


def cents_between(hz_a: float, hz_b: float) -> float:
    return 1200.0 * math.log2(hz_a / hz_b)


# --------------------------------------------------------------------------
# YIN
# --------------------------------------------------------------------------
def _difference_function(x: np.ndarray, tau_max: int) -> np.ndarray:
    """YIN 差分函数 d(tau)，用 FFT 自相关加速。

    d(tau) = Σ_{j=0}^{W-1} (x[j] - x[j+tau])²
           = Σ x[j]² + Σ x[j+tau]² - 2·Σ x[j]·x[j+tau]
    其中 W = len(x) - tau_max 固定，保证各项长度一致。
    """
    n = len(x)
    w = n - tau_max
    if w <= 0:
        raise ValueError("分析窗口太短：需要 len(x) > tau_max")

    power = np.concatenate(([0.0], np.cumsum(x * x)))
    term_a = power[w] - power[0]
    taus = np.arange(tau_max + 1)
    term_b = power[taus + w] - power[taus]

    # 互相关 r(tau) = Σ_{j=0}^{W-1} x[j]·x[j+tau]，用 FFT 卷积实现
    size = 1 << int(math.ceil(math.log2(n + w)))
    seg = x[:w][::-1]
    conv = np.fft.irfft(np.fft.rfft(x, size) * np.fft.rfft(seg, size), size)
    r = conv[taus + w - 1]

    return term_a + term_b - 2.0 * r


def _cmndf(d: np.ndarray) -> np.ndarray:
    """累积均值归一化差分函数 d'(tau)。"""
    out = np.ones_like(d)
    running = 0.0
    for tau in range(1, len(d)):
        running += d[tau]
        out[tau] = d[tau] * tau / running if running > 0 else 1.0
    return out


def _parabolic_min(y: np.ndarray, tau: int) -> float:
    """抛物线插值，把整数滞后细化到小数，提升音分精度。"""
    if tau <= 0 or tau >= len(y) - 1:
        return float(tau)
    s0, s1, s2 = y[tau - 1], y[tau], y[tau + 1]
    denom = 2.0 * (2.0 * s1 - s2 - s0)
    if abs(denom) < 1e-12:
        return float(tau)
    return tau + (s2 - s0) / denom


def yin_pitch(
    x: np.ndarray,
    sr: int,
    fmin: float = 70.0,
    fmax: float = 1400.0,
    threshold: float = 0.12,
) -> tuple[float, float]:
    """估计基频。

    返回 (频率 Hz, 置信度)。置信度 = 1 - d'(tau*)；<=0 表示无有效音高。
    """
    tau_min = max(1, int(sr / fmax))
    tau_max = min(len(x) // 2, int(sr / fmin))
    if tau_max <= tau_min:
        return 0.0, 0.0

    d = _difference_function(x, tau_max)
    d[0] = 0.0
    cmnd = _cmndf(d)

    # 绝对阈值：取第一个低于阈值且为局部极小值的 tau
    tau_est = -1
    for tau in range(tau_min, tau_max):
        if cmnd[tau] < threshold and cmnd[tau] <= cmnd[tau + 1]:
            tau_est = tau
            break
    if tau_est < 0:
        tau_est = int(np.argmin(cmnd[tau_min:tau_max]) + tau_min)
        if cmnd[tau_est] > 0.60:  # 太不清晰，视为无音高
            return 0.0, 0.0

    tau_refined = _parabolic_min(cmnd, tau_est)
    hz = sr / tau_refined if tau_refined > 0 else 0.0
    confidence = float(max(0.0, 1.0 - cmnd[tau_est]))
    return hz, confidence


def harmonic_correct(
    x: np.ndarray,
    sr: int,
    hz: float,
    min_check_hz: float = 200.0,
    min_bin_sep: float = 4.0,
) -> tuple[float, bool]:
    """谐波校验：抑制"把二次谐波当作基频"造成的八度升高误判。

    关键约束（原型测试得出的教训）：
      只有当 f0 与 f0/2 在频谱上**真的分得开**时，这个判断才可靠。短窗口下
      FFT 分辨率不足（例如 2048 点 @48kHz 时 bin=23.4Hz，而 E2 的 f0/2 与 f0
      只相隔 1.76 个 bin），基频主瓣泄漏会被误判成"存在低八度成分"，反而把
      正确的基频改错。因此：

        - f0 < min_check_hz（默认 200Hz）时**跳过**校验，直接信任 YIN；
        - f0/2 与 f0 的间隔小于 min_bin_sep 个频点时**跳过**校验；
        - 要求 f0/2 处存在**局部谱峰**（而不是频带内的最大值）。

    返回 (修正后的频率, 是否发生修正)。
    """
    if hz <= 0 or hz < min_check_hz:
        return hz, False

    n = 1 << int(math.ceil(math.log2(len(x))))
    bin_hz = sr / n
    if (hz / 2.0) / bin_hz < min_bin_sep:
        return hz, False

    spec = np.abs(np.fft.rfft(x * np.hanning(len(x)), n))

    def peak_at(f: float) -> float:
        """取 f 附近的局部谱峰幅度；找不到局部峰则返回 0。"""
        idx = int(round(f / bin_hz))
        lo, hi = max(1, idx - 2), min(len(spec) - 1, idx + 3)
        if hi <= lo:
            return 0.0
        local = int(np.argmax(spec[lo:hi])) + lo
        if not (spec[local] > spec[local - 1] and spec[local] >= spec[local + 1]):
            return 0.0
        return float(spec[local])

    e_here = peak_at(hz)
    e_lower = peak_at(hz / 2.0)
    # 只有当低八度成分**明显更强**时才改写，避免误伤
    if e_lower > 1.2 * e_here and e_lower > 0.0:
        return hz / 2.0, True
    return hz, False


# --------------------------------------------------------------------------
# 合成吉他音（用于基准测试，无需真实录音）
# --------------------------------------------------------------------------
def synth_guitar_note(
    f0: float,
    sr: int,
    dur: float = 1.0,
    n_harmonics: int = 12,
    decay: float = 4.0,
    noise_db: float | None = None,
    seed: int = 0,
) -> np.ndarray:
    """合成一个拨弦音：谐波幅度 ∝ 1/n^1.2，指数衰减，可叠加白噪声。"""
    t = np.arange(int(sr * dur)) / sr
    sig = np.zeros_like(t)
    for n in range(1, n_harmonics + 1):
        f = f0 * n
        if f > sr / 2 * 0.95:
            break
        amp = 1.0 / (n ** 1.2)
        # 高次谐波衰减更快，模拟真实琴弦
        sig += amp * np.sin(2 * np.pi * f * t + 0.3 * n) * np.exp(-decay * t * (1 + 0.25 * (n - 1)))
    sig /= np.max(np.abs(sig)) + 1e-12
    if noise_db is not None:
        rng = np.random.default_rng(seed)
        noise = rng.normal(0, 10 ** (-noise_db / 20.0), size=sig.shape)
        sig = sig + noise
    return sig


# --------------------------------------------------------------------------
# 基准测试
# --------------------------------------------------------------------------
@dataclass
class Case:
    label: str
    f0: float
    window: int
    noise_db: float | None
    raw_hz: float
    raw_cents: float
    detected: float
    cents: float
    confidence: float
    octave_fixed: bool


def run_benchmark(windows=(1024, 2048, 4096), snrs=(None, 20.0, 10.0)) -> dict:
    sr = 48000
    # 六根空弦 + 一个高把位音
    notes = [
        ("6弦空弦 E2", midi_to_hz(40)),
        ("5弦空弦 A2", midi_to_hz(45)),
        ("4弦空弦 D3", midi_to_hz(50)),
        ("3弦空弦 G3", midi_to_hz(55)),
        ("2弦空弦 B3", midi_to_hz(59)),
        ("1弦空弦 E4", midi_to_hz(64)),
        ("1弦12品 E5", midi_to_hz(76)),
    ]

    cases: list[Case] = []
    timings: dict[int, float] = {}

    for window in windows:
        # 取一个稳定段（避开起音），长度 = window
        for label, f0 in notes:
            for snr in snrs:
                sig = synth_guitar_note(f0, sr, dur=1.0, noise_db=snr, seed=40 + int(f0))
                start = int(0.15 * sr)
                frame = sig[start:start + window]
                if len(frame) < window:
                    frame = np.pad(frame, (0, window - len(frame)))

                t0 = time.perf_counter()
                hz, conf = yin_pitch(frame.astype(np.float64), sr)
                elapsed = (time.perf_counter() - t0) * 1000.0
                timings.setdefault(window, []).append(elapsed)

                raw_hz = hz
                raw_cents = cents_between(raw_hz, f0) if raw_hz > 0 else float("nan")

                fixed = False
                if hz > 0:
                    hz2, fixed = harmonic_correct(frame.astype(np.float64), sr, hz)
                    hz = hz2

                cents = cents_between(hz, f0) if hz > 0 else float("nan")
                cases.append(
                    Case(
                        label=label,
                        f0=round(f0, 2),
                        window=window,
                        noise_db=snr if snr is not None else 0.0,
                        raw_hz=round(raw_hz, 2),
                        raw_cents=round(raw_cents, 2) if raw_hz > 0 else float("nan"),
                        detected=round(hz, 2),
                        cents=round(cents, 2) if hz > 0 else float("nan"),
                        confidence=round(conf, 3),
                        octave_fixed=fixed,
                    )
                )

    # 汇总
    summary = []
    for window in windows:
        row = {"window": window, "avg_ms": round(float(np.mean(timings[window])), 1)}
        for snr in snrs:
            key = snr if snr is not None else 0.0
            subset = [c for c in cases if c.window == window and c.noise_db == key]
            ok = [c for c in subset if abs(c.cents) <= 25.0]
            oct_ok = [c for c in subset if abs(c.cents) <= 50.0]
            row[f"acc_{int(key)}db"] = f"{len(ok)}/{len(subset)}"
            row[f"within50_{int(key)}db"] = f"{len(oct_ok)}/{len(subset)}"
        summary.append(row)

    return {"cases": [asdict(c) for c in cases], "summary": summary, "sr": sr}


def run_decision_benchmark(
    window: int = 4096,
    hop: int = 480,
    stable_ms: int = 300,
    conf_min: float = 0.80,
    vote_ratio: float = 0.70,
    snrs=(None, 30.0, 20.0, 15.0, 10.0),
) -> list[dict]:
    """仿真真实判定路径：滑动窗口 + 多数表决 + 稳定时长确认。

    为什么不用"连续 N 帧都合格"：噪声下只要一帧置信度掉下去就会重置计数，
    导致明明单帧识别正确却永远凑不满连续帧（原型测试中 20dB 噪声下 7 个音
    全部判定失败）。改为在最近 N 帧的滑动窗口内做多数表决，容忍偶发丢帧。

    参数对应：N = 稳定时长 / 帧移；vote_ratio = 众数占有效帧的最低比例。
    """
    sr = 48000
    notes = [
        ("6弦空弦 E2", midi_to_hz(40)),
        ("5弦空弦 A2", midi_to_hz(45)),
        ("4弦空弦 D3", midi_to_hz(50)),
        ("3弦空弦 G3", midi_to_hz(55)),
        ("2弦空弦 B3", midi_to_hz(59)),
        ("1弦空弦 E4", midi_to_hz(64)),
        ("1弦12品 E5", midi_to_hz(76)),
    ]
    needed_frames = max(1, int(round(stable_ms / (hop / sr * 1000.0))))

    rows = []
    for snr in snrs:
        ok = 0
        wrong = 0
        no_decision = 0
        latencies = []
        for label, f0 in notes:
            sig = synth_guitar_note(
                f0, sr, dur=1.5, noise_db=snr, seed=int(f0) + (0 if snr is None else int(snr))
            )
            target_pc = round(hz_to_midi(f0)) % 12

            buf: list[int | None] = []
            decided = None
            decide_time = None

            for start in range(int(0.05 * sr), len(sig) - window, hop):
                frame = sig[start:start + window].astype(np.float64)
                hz, conf = yin_pitch(frame, sr)
                if hz > 0 and conf >= conf_min:
                    hz, _ = harmonic_correct(frame, sr, hz)
                    pc = int(round(hz_to_midi(hz))) % 12
                    buf.append(pc)
                else:
                    buf.append(None)
                if len(buf) > needed_frames:
                    buf.pop(0)

                valid = [b for b in buf if b is not None]
                if len(buf) >= needed_frames and len(valid) >= max(3, int(0.4 * needed_frames)):
                    counts: dict[int, int] = {}
                    for pc in valid:
                        counts[pc] = counts.get(pc, 0) + 1
                    pc_mode, cnt = max(counts.items(), key=lambda kv: kv[1])
                    if cnt >= vote_ratio * len(valid):
                        decided = pc_mode
                        decide_time = (start + window) / sr
                        break

            if decided == target_pc:
                ok += 1
                if decide_time is not None:
                    latencies.append(decide_time * 1000.0)
            elif decided is None:
                no_decision += 1
            else:
                wrong += 1

        rows.append(
            {
                "snr": snr,
                "correct": f"{ok}/{len(notes)}",
                "wrong": wrong,
                "no_decision": no_decision,
                "avg_latency_ms": round(float(np.mean(latencies)), 1) if latencies else None,
                "needed_frames": needed_frames,
            }
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="YIN 音高检测基准测试")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    result = run_benchmark()

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    print("## 分析窗口与噪声对识别准确率的影响\n")
    print("（判对标准：|音分偏差| <= 25 音分，采样率 48kHz，合成吉他音，已含谐波校验）\n")
    print("| 窗口 | 单次耗时 | 无噪声 | 20dB 噪声 | 10dB 噪声 |")
    print("|---|---|---|---|---|")
    for row in result["summary"]:
        print(
            f"| {row['window']} | {row['avg_ms']} ms | {row['acc_0db']} | "
            f"{row['acc_20db']} | {row['acc_10db']} |"
        )

    # 窗口下限约束：tau_max = min(window//2, sr/fmin)
    print("\n## 窗口下限约束（fmin=70Hz, sr=48kHz → 需要 tau_max >= 685）\n")
    print("| 窗口 | tau_max | E2 周期 582 是否可表示 |")
    print("|---|---|---|")
    for window in (1024, 2048, 4096):
        tau_max = min(window // 2, int(result["sr"] / 70.0))
        ok = "是" if tau_max >= 583 else "**否（YIN 无解）**"
        print(f"| {window} | {tau_max} | {ok} |")

    # 各音在每个窗口的原始 YIN 表现（无噪声）
    print("\n## 各音原始 YIN 精度（无噪声，未做谐波校验）\n")
    print("| 音 | 真值(Hz) | 1024 窗口 | 2048 窗口 | 4096 窗口 |")
    print("|---|---|---|---|---|")
    labels = []
    for c in result["cases"]:
        if c["label"] not in labels:
            labels.append(c["label"])
    for label in labels:
        row = {c["window"]: c for c in result["cases"] if c["label"] == label and c["noise_db"] == 0.0}
        f0 = row[2048]["f0"] if 2048 in row else 0.0
        cells = []
        for window in (1024, 2048, 4096):
            c = row.get(window)
            if c is None:
                cells.append("-")
            elif abs(c["raw_cents"]) > 100:
                cells.append(f"**{c['raw_hz']:.2f} ({c['raw_cents']:+.0f}音分)**")
            else:
                cells.append(f"{c['raw_hz']:.2f} ({c['raw_cents']:+.1f}音分)")
        print(f"| {label} | {f0:.2f} | {cells[0]} | {cells[1]} | {cells[2]} |")

    octave_fixes = [c for c in result["cases"] if c["octave_fixed"]]
    print(f"\n谐波校验触发次数：{len(octave_fixes)}（共 {len(result['cases'])} 个测试样本）")

    # 真实判定路径仿真
    decision = run_decision_benchmark()
    print("\n## 真实判定路径仿真（窗口 4096、帧移 10ms、稳定时长 300ms、置信度 >= 0.8）\n")
    print("| 噪声水平 | 判定正确 | 平均判定延迟 |")
    print("|---|---|---|")
    for row in decision:
        snr_label = "无噪声" if row["snr"] is None else f"{int(row['snr'])} dB"
        lat = f"{row['avg_latency_ms']} ms" if row["avg_latency_ms"] else "-"
        print(f"| {snr_label} | {row['correct']} | {lat} |")

    print("\n> 噪声定义：白噪声标准差相对信号峰值的比值，20dB = 0.1，10dB = 0.32。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
