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

    两条判据满足其一即算起音：

    1. **突跳**：当前帧电平比最近 ``history`` 帧的**最大值**高出 ``rise_db`` 以上。
       用最大值而不是均值作基线，是为了不被"缓升"骗到：如果音量在几帧内慢慢爬升
       （例如远处的环境声渐强），相对最近最大值的增幅很小，不会误判为拨弦。
    2. **从近期谷底回升**：当前帧比最近 ``floor_history`` 帧的**最小值**高出
       ``floor_rise_db`` 以上。

    为什么需要第 2 条（实测反馈："6 弦、1 弦空弹捕捉不敏感"）
    ------------------------------------------------------

    只靠第 1 条时，**琴弦还在余振中重新拨响会被漏检**：此时最近几帧的电平本来就
    不低，重拨带来的增幅常常不到 8 dB。而用户弹空弦（不用按品）时往往连续快速拨弦，
    正是这种情况 —— 起音漏检 → 判定器一直处于"未武装"状态 → 用户怎么弹都没反应。

    衰减中的琴弦在最近若干帧里是**单调下降**的，因此"最近 150ms 的最小值"
    就是最新的一帧；重拨会让电平明显回升，从而被第 2 条判据抓住。
    稳定音上最小值≈最大值，不会误触发；缓慢的环境噪声抬升也达不到阈值。
    """

    rise_db: float = 8.0
    history: int = 3
    #: 用来取"近期谷底"的帧数（默认 15 帧 ≈ 150ms @ 10ms 帧移）
    floor_history: int = 15
    #: 相对近期谷底的回升阈值
    floor_rise_db: float = 6.0
    #: 谷底判据要求的"相对上一帧的即时抬升"，用来排除衰减过程中的延迟误报
    floor_step_db: float = 3.0
    _recent: list[float] = field(default_factory=list)
    _floor: list[float] = field(default_factory=list)

    def update(self, level_db: float) -> bool:
        """送入一帧电平，返回是否为起音。"""
        previous = self._floor[-1] if self._floor else None
        is_onset = False
        if len(self._recent) >= self.history:
            baseline = max(self._recent)
            if level_db - baseline >= self.rise_db:
                is_onset = True
        if (
            not is_onset
            and len(self._floor) >= self.floor_history
            and previous is not None
            and level_db - previous >= self.floor_step_db  # 必须是在"往上走"
            and level_db - min(self._floor) >= self.floor_rise_db
        ):
            is_onset = True

        if is_onset:
            # 触发后重建基线，避免持续音被反复判为起音
            self._recent.clear()
            self._floor.clear()
        self._recent.append(level_db)
        if len(self._recent) > self.history:
            self._recent.pop(0)
        self._floor.append(level_db)
        if len(self._floor) > self.floor_history:
            self._floor.pop(0)
        return is_onset

    def reset(self) -> None:
        self._recent.clear()
        self._floor.clear()


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
