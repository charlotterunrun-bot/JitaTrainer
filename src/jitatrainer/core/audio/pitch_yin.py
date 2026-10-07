"""YIN 音高检测（正式实现）。

算法与参数来自 M0 基准测试（tools/pitch_bench.py）的实测结论：

  - 分析窗口 2048 帧（43ms @48kHz）：必须 >= 2 * sr / fmin = 1371 帧，
    否则 E2 在物理上无法被测出（实测 1024 窗口会输出 39.29Hz 的错误值）
  - YIN 绝对阈值 0.12，置信度下限 0.80
  - 谐波校验采用**保守策略**：只在频率足够高、频谱分辨率足够时才启用，
    避免把正确基频改错（第一版激进规则在 63 个样本中误伤 14 次）

本模块不依赖 PySide6，可独立单元测试。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from ..theory.notes import cents_between, hz_to_midi, nearest_cents_offset, pitch_class

#: 采样率缺省值
DEFAULT_SAMPLERATE = 48000


@dataclass(frozen=True, slots=True)
class YinConfig:
    """YIN 参数（默认值即实测选定值）。"""

    samplerate: int = DEFAULT_SAMPLERATE
    window: int = 2048
    fmin: float = 70.0
    fmax: float = 1400.0
    threshold: float = 0.12
    confidence_min: float = 0.80
    #: 谐波校验仅在 f0 不低于该值时启用
    harmonic_check_min_hz: float = 200.0
    #: f0/2 与 f0 至少相隔多少个频点才允许校验
    harmonic_min_bin_sep: float = 4.0

    @property
    def tau_max(self) -> int:
        """差分函数的最大滞后。"""
        return min(self.window // 2, int(self.samplerate / self.fmin))

    @property
    def tau_min(self) -> int:
        return max(1, int(self.samplerate / self.fmax))

    def validate(self) -> None:
        """检查窗口是否满足最低频率的硬约束。"""
        required = 2 * self.samplerate / self.fmin
        if self.window < required:
            raise ValueError(
                f"分析窗口 {self.window} 帧不足以检测 {self.fmin}Hz："
                f"至少需要 {required:.0f} 帧（2 × 采样率 / 最低频率）"
            )
        if self.tau_max <= self.tau_min:
            raise ValueError("fmin/fmax 设置区间过窄，无法检测")


@dataclass(frozen=True, slots=True)
class PitchResult:
    """一次检测的结果。

    ``accepted`` 表示是否达到置信度门限。**只有 accepted 的结果才是可用答案**：
    噪声下 YIN 仍可能给出一个自信度很低的频率（实测 10dB 噪声下把 B3 读成 83Hz），
    如果把这种读数当成有效结果，就会产生"冤判"。因此 ``valid`` 同时要求
    accepted 与频率为正。
    """

    hz: float = 0.0
    confidence: float = 0.0
    accepted: bool = False
    harmonic_corrected: bool = False

    @property
    def valid(self) -> bool:
        return bool(self.accepted and self.hz > 0.0)

    @property
    def midi(self) -> float:
        return hz_to_midi(self.hz) if self.valid else float("nan")

    @property
    def pitch_class(self) -> int | None:
        return pitch_class(round(self.midi)) if self.valid else None

    @property
    def cents(self) -> float:
        """与最近同名音的音分偏差。"""
        if not self.valid:
            return float("nan")
        pc = self.pitch_class
        if pc is None:
            return float("nan")
        return nearest_cents_offset(self.hz, pc)


def difference_function(x: np.ndarray, tau_max: int) -> np.ndarray:
    """YIN 差分函数 d(tau)，用 FFT 互相关加速（避免 O(N^2)）。"""
    n = len(x)
    w = n - tau_max
    if w <= 0:
        raise ValueError("分析窗口太短：需要 len(x) > tau_max")

    power = np.concatenate(([0.0], np.cumsum(x * x)))
    taus = np.arange(tau_max + 1)
    term_a = power[w]
    term_b = power[taus + w] - power[taus]

    size = 1 << int(math.ceil(math.log2(n + w)))
    segment = x[:w][::-1]
    conv = np.fft.irfft(np.fft.rfft(x, size) * np.fft.rfft(segment, size), size)
    cross = conv[taus + w - 1]

    return term_a + term_b - 2.0 * cross


def cumulative_mean_normalized(d: np.ndarray) -> np.ndarray:
    """累积均值归一化差分函数 d'(tau)。"""
    out = np.ones_like(d)
    running = 0.0
    for tau in range(1, len(d)):
        running += d[tau]
        out[tau] = d[tau] * tau / running if running > 0 else 1.0
    return out


def _parabolic_min(y: np.ndarray, tau: int) -> float:
    """抛物线插值，把整数滞后细化为小数以提升音分精度。"""
    if tau <= 0 or tau >= len(y) - 1:
        return float(tau)
    s0, s1, s2 = y[tau - 1], y[tau], y[tau + 1]
    denom = 2.0 * (2.0 * s1 - s2 - s0)
    if abs(denom) < 1e-12:
        return float(tau)
    return tau + (s2 - s0) / denom


def harmonic_correct(
    frame: np.ndarray,
    samplerate: int,
    hz: float,
    *,
    min_check_hz: float = 200.0,
    min_bin_sep: float = 4.0,
    low_max_hz: float = 420.0,
    low_presence_ratio: float = 0.02,
    low_floor_factor: float = 3.0,
    min_low_hz: float = 60.0,
    fmin: float = 70.0,
    low_divisors: tuple[int, ...] = (2, 3),
) -> tuple[float, bool]:
    """谐波校验：抑制 YIN 的"谐波锁定"误判。

    **A. 低频/中频区的谐波锁定（真实录音实测发现）**

    麦克风低频响应差、或拨弦位置偏桥时，基频可能远弱于某个高次谐波。
    2026-10-07 的真实录音里出现过两种：

      - 第 6 弦：基频 78Hz 的幅度只有二次谐波 155Hz 的 **1/18** → YIN 报高八度；
      - 第 5 弦：基频 107Hz 的幅度只有三次谐波 318Hz 的 **1/19** → YIN 报十二度。

    这类情况下候选基频（hz/2、hz/3）在频谱里**确实有峰，只是很弱**，
    因此判据用"存在性"而不是"更强"，并要求：

      1. 候选基频落在吉他音域内（>= max(min_low_hz, fmin)）；
      2. 候选处存在局部谱峰，幅度 > low_presence_ratio × 当前读数处幅度，
         且 > low_floor_factor × 频带中位幅度（排除噪声里的偶发隆起）；
      3. **候选基频的二次谐波处也有支持** —— 真正的低音才有这个特征，
         这一条用于排除"随手把高音拉低"的误伤；
      4. 多个候选都成立时取**最低**的那个。

    **B. 高频区的"把二次谐波当基频"**

    这里反而要**保守**：第一版规则用"频带能量 > 50%"判断，在 63 个样本里
    误伤 14 次（短窗口下基频主瓣泄漏被当成低八度成分，把正确的音改错）。
    修正后要求 f0 与 f0/2 在频谱上真的分得开（至少 min_bin_sep 个频点），
    并要求 f0/2 处的局部谱峰显著强于 f0（> 1.2 倍）。

    Returns:
        (修正后的频率, 是否发生修正)
    """
    if hz <= 0:
        return hz, False

    # 补零到 4 倍窗口：不增加真实分辨率，但让谱峰定位精确得多
    n = 1 << int(math.ceil(math.log2(max(len(frame), 1) * 4)))
    bin_hz = samplerate / n
    spectrum = np.abs(np.fft.rfft(frame * np.hanning(len(frame)), n))

    def peak_at(freq: float, tol_ratio: float = 0.05) -> float:
        """freq 附近 ±tol 范围内的局部谱峰幅度；没有局部峰则返回 0。"""
        if freq <= 0:
            return 0.0
        half_width = max(1, int(round(freq * tol_ratio / bin_hz)))
        center = int(round(freq / bin_hz))
        lo = max(1, center - half_width)
        hi = min(len(spectrum) - 1, center + half_width + 1)
        if hi <= lo:
            return 0.0
        local = int(np.argmax(spectrum[lo:hi])) + lo
        if not (spectrum[local] > spectrum[local - 1] and spectrum[local] >= spectrum[local + 1]):
            return 0.0
        return float(spectrum[local])

    # ---------------- A. 低频/中频区：按约数向下搜索真实基频 ----------------
    if hz <= low_max_hz:
        energy_here = peak_at(hz)
        band = spectrum[max(1, int(60 / bin_hz)) : min(len(spectrum), int(1200 / bin_hz))]
        band_median = float(np.median(band)) if band.size else 0.0
        min_candidate = max(min_low_hz, fmin)

        best: float | None = None
        for divisor in low_divisors:
            candidate = hz / divisor
            if candidate < min_candidate:
                continue
            presence = peak_at(candidate)
            if presence <= 0.0:
                continue
            if energy_here > 0.0 and presence < low_presence_ratio * energy_here:
                continue
            if presence < low_floor_factor * band_median:
                continue
            second = peak_at(candidate * 2.0)
            if second <= 0.0 or (energy_here > 0.0 and second < low_presence_ratio * energy_here):
                continue
            best = candidate  # 继续循环，取更低的候选

        if best is not None:
            return best, True
        return hz, False

    # ---------------- B. 高频区：保守的向下修正 ----------------
    if hz < min_check_hz:
        return hz, False
    if (hz / 2.0) / bin_hz < min_bin_sep:
        return hz, False

    energy_here = peak_at(hz)
    energy_lower = peak_at(hz / 2.0)
    if energy_lower > 1.2 * energy_here and energy_lower > 0.0:
        return hz / 2.0, True
    return hz, False


class PitchDetector:
    """封装参数与状态的音高检测器。"""

    def __init__(self, config: YinConfig | None = None) -> None:
        self.config = config or YinConfig()
        self.config.validate()

    def detect(self, frame: np.ndarray) -> PitchResult:
        """对一帧音频做检测。frame 长度应等于 config.window。"""
        cfg = self.config
        if len(frame) < cfg.window:
            frame = np.pad(frame, (0, cfg.window - len(frame)))
        elif len(frame) > cfg.window:
            frame = frame[: cfg.window]

        signal = np.asarray(frame, dtype=np.float64)
        if not np.any(signal):
            return PitchResult()

        tau_min, tau_max = cfg.tau_min, cfg.tau_max
        d = difference_function(signal, tau_max)
        d[0] = 0.0
        cmnd = cumulative_mean_normalized(d)

        tau_est = -1
        for tau in range(tau_min, tau_max):
            if cmnd[tau] < cfg.threshold and cmnd[tau] <= cmnd[tau + 1]:
                tau_est = tau
                break
        if tau_est < 0:
            tau_est = int(np.argmin(cmnd[tau_min:tau_max]) + tau_min)
            if cmnd[tau_est] > 0.60:
                return PitchResult()

        tau_refined = _parabolic_min(cmnd, tau_est)
        if tau_refined <= 0:
            return PitchResult()
        hz = cfg.samplerate / tau_refined
        confidence = float(max(0.0, 1.0 - cmnd[tau_est]))
        if confidence < cfg.confidence_min:
            # 保留原始读数仅供诊断，但标记为未被接受（不得参与判定）
            return PitchResult(hz=float(hz), confidence=confidence, accepted=False)

        corrected_hz, was_corrected = harmonic_correct(
            signal,
            cfg.samplerate,
            hz,
            min_check_hz=cfg.harmonic_check_min_hz,
            min_bin_sep=cfg.harmonic_min_bin_sep,
        )
        return PitchResult(
            hz=float(corrected_hz),
            confidence=confidence,
            accepted=True,
            harmonic_corrected=was_corrected,
        )


def cents_error(hz: float, target_hz: float) -> float:
    """便捷函数：相对目标频率的音分误差。"""
    return cents_between(hz, target_hz)
