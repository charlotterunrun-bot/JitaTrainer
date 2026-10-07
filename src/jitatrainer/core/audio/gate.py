"""起音门限与环境噪声地板。

分两层：
  - **纯计算**（本模块）：音量换算、噪声地板估计、门限建议、起音检测。
    全部可在没有音频设备的情况下单元测试。
  - **采集**（capture.py）：把音频块送进来。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

#: 低于此值的帧直接视作静音，不进入音高检测（省 CPU，也避免噪声被当成音）
MIN_GATE_DB = -80.0
MAX_GATE_DB = -10.0

SILENCE_DB = -120.0


def frame_rms_db(frame: np.ndarray) -> float:
    """一帧音频的 RMS 电平（dBFS）。全幅正弦波约为 -3 dB。"""
    data = np.asarray(frame, dtype=np.float64)
    if data.size == 0:
        return SILENCE_DB
    rms = float(np.sqrt(np.mean(data * data)))
    if rms <= 1e-12:
        return SILENCE_DB
    return 20.0 * float(np.log10(rms))


@dataclass(frozen=True, slots=True)
class GateConfig:
    """门限配置。"""

    noise_floor_db: float = -60.0
    #: 门限相对噪声地板的抬升量
    offset_db: float = 12.0
    #: 起音判定：相对前几帧均值上升多少 dB
    onset_rise_db: float = 8.0

    @property
    def gate_db(self) -> float:
        return clamp_gate(self.noise_floor_db + self.offset_db)


def clamp_gate(value: float) -> float:
    return float(min(MAX_GATE_DB, max(MIN_GATE_DB, value)))


class NoiseFloorMeter:
    """环境噪声地板估计。

    用**分位数**而不是最小值/均值：用户可能在测量期间碰到琴弦，
    单个大音量帧不应该把地板抬高。
    """

    def __init__(self, percentile: float = 25.0) -> None:
        if not 0.0 <= percentile <= 100.0:
            raise ValueError("分位数必须在 0..100")
        self.percentile = percentile
        self._levels: list[float] = []

    def add(self, frame: np.ndarray) -> float:
        level = frame_rms_db(frame)
        self._levels.append(level)
        return level

    def add_level(self, level_db: float) -> None:
        self._levels.append(float(level_db))

    @property
    def count(self) -> int:
        return len(self._levels)

    @property
    def floor_db(self) -> float:
        if not self._levels:
            return SILENCE_DB
        return float(np.percentile(self._levels, self.percentile))

    def suggest_gate(self, offset_db: float = 12.0) -> float:
        return clamp_gate(self.floor_db + offset_db)

    def reset(self) -> None:
        self._levels.clear()


@dataclass
class OnsetDetector:
    """拨弦起音检测。

    判据：当前帧电平比最近 ``history`` 帧的**最大值**高出 ``rise_db`` 以上。

    用最大值而不是均值作基线，是为了不被"缓升"骗到：如果音量在几帧内
    慢慢爬升（例如远处的环境声渐强），相对最近最大值的增幅很小，不会误判为
    拨弦；而真正的拨弦是相对前几帧的突然跳变。

    起音时刻用于标定"用户开始弹奏"，是宽容期结束后判定窗口的起点参考。
    """

    rise_db: float = 8.0
    history: int = 3
    _recent: list[float] = field(default_factory=list)

    def update(self, level_db: float) -> bool:
        """送入一帧电平，返回是否为起音。"""
        is_onset = False
        if len(self._recent) >= self.history:
            baseline = max(self._recent)
            if level_db - baseline >= self.rise_db:
                is_onset = True

        if is_onset:
            # 触发后重建基线，避免持续音被反复判为起音
            self._recent.clear()
        self._recent.append(level_db)
        if len(self._recent) > self.history:
            self._recent.pop(0)
        return is_onset

    def reset(self) -> None:
        self._recent.clear()


@dataclass(frozen=True, slots=True)
class CaptureStats:
    """一次采集探测的统计结果。"""

    frames: int
    peak_db: float
    rms_db: float
    clipped: bool
    gate_db: float

    @property
    def has_signal(self) -> bool:
        return self.rms_db > self.gate_db


def summarize_capture(levels: list[float], gate_db: float) -> CaptureStats:
    """把一组帧电平汇总成探测结果。"""
    if not levels:
        return CaptureStats(0, SILENCE_DB, SILENCE_DB, False, gate_db)
    peak = float(max(levels))
    # 用能量平均更贴近听感
    rms = float(10.0 * np.log10(np.mean([10.0 ** (level / 10.0) for level in levels])))
    return CaptureStats(len(levels), peak, rms, peak >= -1.0, gate_db)
