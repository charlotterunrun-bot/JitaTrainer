"""分析器：把音频帧变成 PitchEvent，并提供驱动线程。

设计上刻意分成两半，便于测试：

  - ``FrameAnalyzer.process(frame, t)``：**纯函数式**，输入一帧音频返回一个
    PitchEvent。不需要音频设备，可以用合成音频做完整单元测试。
  - ``AnalyzerThread``：按帧移从环形缓冲取窗、调用 FrameAnalyzer、把结果推给
    回调。线程里不做任何重活。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from .events import PitchEvent
from .gate import GateConfig, OnsetDetector, SILENCE_DB, frame_rms_db
from .pitch_yin import PitchDetector, YinConfig
from .ring import RingBuffer


@dataclass(frozen=True, slots=True)
class AnalyzerConfig:
    window: int = 2048
    hop: int = 480
    samplerate: int = 48000
    #: 低频增强：低音弦的基频在短窗口下分辨率不足，YIN 会锁到二次谐波。
    #: 实测（用户实录，2026-10-07）：2048 窗口把 E2 读成 D#3（157Hz），
    #: 8192 窗口能正确读到 79.2Hz。因此对低频段启用一条更长的分析窗口。
    low_enhance: bool = True
    low_window: int = 8192
    #: 低于该频率的检测结果会尝试用长窗口复核
    low_band_hz: float = 220.0

    @property
    def hop_ms(self) -> float:
        return self.hop / self.samplerate * 1000.0

    @property
    def window_ms(self) -> float:
        return self.window / self.samplerate * 1000.0

    @property
    def low_window_ms(self) -> float:
        return self.low_window / self.samplerate * 1000.0

    @property
    def required_samples(self) -> int:
        """分析线程每次需要从环形缓冲取出的样本数。"""
        return self.low_window if self.low_enhance else self.window


class FrameAnalyzer:
    """单帧处理：门限 → 起音 → YIN → （低频段）长窗口复核 → PitchEvent。"""

    def __init__(
        self,
        config: AnalyzerConfig | None = None,
        gate: GateConfig | None = None,
        detector: PitchDetector | None = None,
    ) -> None:
        self.config = config or AnalyzerConfig()
        self.gate = gate or GateConfig()
        self.detector = detector or PitchDetector(
            YinConfig(samplerate=self.config.samplerate, window=self.config.window)
        )
        self._onset = OnsetDetector(rise_db=self.gate.onset_rise_db)
        #: 低频段复核用的长窗口检测器
        self.low_detector: PitchDetector | None = None
        if self.config.low_enhance and self.config.low_window > self.config.window:
            self.low_detector = PitchDetector(
                YinConfig(samplerate=self.config.samplerate, window=self.config.low_window)
            )
        #: 整体检测偏差校正（音分）。由向导的试弹校准测得，用于补偿系统性偏差；
        #: 注意它**不**应用来掩盖琴本身没调准的问题。
        self.detune_cents: float = 0.0

    @property
    def gate_db(self) -> float:
        return self.gate.gate_db

    def update_gate(self, gate: GateConfig) -> None:
        self.gate = gate
        self._onset.rise_db = gate.onset_rise_db

    def set_detune_cents(self, cents: float) -> None:
        self.detune_cents = float(cents)

    def _enhance_low(self, frame: np.ndarray, fast_hz: float, fast_confidence: float):
        """低频段复核：用长窗口再测一次，若给出更低且仍然可靠的基频则采用。

        低音弦的基频在短窗口下分辨不出，YIN 会锁到二次谐波（E2 → E3）。
        长窗口能分辨出真实基频，因此当快路径结果落在低频段时再做一次。
        """
        if self.low_detector is None:
            return None
        if not (0.0 < fast_hz <= self.config.low_band_hz):
            return None
        if frame.size < self.config.low_window:
            return None
        long_frame = np.asarray(frame[-self.config.low_window :], dtype=np.float64)
        result = self.low_detector.detect(long_frame)
        if not result.valid:
            return None
        # 只接受"把八度/十二度拉回真实基频"的方向，且新结果必须落在低频段
        if result.hz >= fast_hz or result.hz > self.config.low_band_hz:
            return None
        if result.confidence < fast_confidence - 0.15:
            return None
        return result

    def process(self, frame: np.ndarray, t: float) -> PitchEvent:
        """处理一帧；返回 PitchEvent（静音帧的 hz 为 0）。

        ``frame`` 可以是长于 ``config.window`` 的缓冲（分析线程会取
        ``required_samples`` 个样本），快路径只用最后 ``window`` 个样本。
        """
        signal = np.asarray(frame, dtype=np.float64)
        level = frame_rms_db(signal[-self.config.window :])
        is_onset = self._onset.update(level)

        if level < self.gate_db:
            # 低于门限：不做 YIN（省 CPU），也不允许被判定
            return PitchEvent(t=t, hz=0.0, confidence=0.0, rms_db=level, is_onset=is_onset)

        fast_frame = signal[-self.config.window :]
        result = self.detector.detect(fast_frame)
        corrected = False
        if not result.valid:
            return PitchEvent(t=t, hz=0.0, confidence=result.confidence, rms_db=level, is_onset=is_onset)

        hz = result.hz
        confidence = result.confidence

        enhanced = self._enhance_low(signal, hz, confidence)
        if enhanced is not None:
            hz = enhanced.hz
            confidence = enhanced.confidence
            corrected = True

        if self.detune_cents:
            hz = hz * (2.0 ** (-self.detune_cents / 1200.0))

        return PitchEvent(
            t=t,
            hz=hz,
            confidence=confidence,
            rms_db=level,
            is_onset=is_onset,
            harmonic_corrected=corrected or result.harmonic_corrected,
        )


@dataclass(eq=False)
class AnalyzerThread(threading.Thread):
    """从环形缓冲按帧移取窗并分析的后台线程。

    注意 ``eq=False``：``threading.Thread`` 会把自己放进一个 WeakSet，
    要求实例**可哈希**；而 dataclass 默认生成 ``__eq__`` 并把 ``__hash__`` 置为 None，
    于是 ``AudioSession.start()`` 一构造它就抛
    ``TypeError: unhashable type: 'AnalyzerThread'``——
    也就是说真实麦克风路径完全起不来（单元测试用的是 FrameAnalyzer，所以没被发现）。
    """

    ring: RingBuffer
    analyzer: FrameAnalyzer
    on_event: Callable[[PitchEvent], None]
    config: AnalyzerConfig = field(default_factory=AnalyzerConfig)
    _stop_event: threading.Event = field(default_factory=threading.Event, repr=False)
    _t0: float | None = field(default=None, repr=False)
    processed: int = 0
    dropped: int = 0

    def __post_init__(self) -> None:
        super().__init__(name="JitaAnalyzer", daemon=True)

    def stop(self) -> None:
        self._stop_event.set()

    @property
    def stopped(self) -> bool:
        return self._stop_event.is_set()

    def tick(self) -> PitchEvent | None:
        """取一窗并处理一次；数据不足时返回 None。供测试直接调用。"""
        frame = self.ring.read_latest(self.config.required_samples)
        if frame is None:
            self.dropped += 1
            return None
        now = time.monotonic()
        if self._t0 is None:
            self._t0 = now
        event = self.analyzer.process(frame[:, 0], now - self._t0)
        self.processed += 1
        return event

    def run(self) -> None:  # pragma: no cover - 线程循环，由集成测试覆盖
        interval = self.config.hop_ms / 1000.0
        next_at = time.monotonic()
        while not self._stop_event.is_set():
            event = self.tick()
            if event is not None:
                try:
                    self.on_event(event)
                except Exception:  # noqa: BLE001 - 回调异常不得终止采集
                    pass
            next_at += interval
            sleep_for = next_at - time.monotonic()
            if sleep_for > 0:
                time.sleep(sleep_for)
            else:
                next_at = time.monotonic()  # 落后了就重新对齐，避免追帧风暴


__all__ = ["AnalyzerConfig", "AnalyzerThread", "FrameAnalyzer", "SILENCE_DB"]
