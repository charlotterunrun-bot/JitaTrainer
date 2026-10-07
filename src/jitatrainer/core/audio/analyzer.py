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

    @property
    def hop_ms(self) -> float:
        return self.hop / self.samplerate * 1000.0

    @property
    def window_ms(self) -> float:
        return self.window / self.samplerate * 1000.0


class FrameAnalyzer:
    """单帧处理：门限 → 起音 → YIN → PitchEvent。"""

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

    def process(self, frame: np.ndarray, t: float) -> PitchEvent:
        """处理一帧；返回 PitchEvent（静音帧的 hz 为 0）。"""
        level = frame_rms_db(frame)
        is_onset = self._onset.update(level)

        if level < self.gate_db:
            # 低于门限：不做 YIN（省 CPU），也不允许被判定
            return PitchEvent(t=t, hz=0.0, confidence=0.0, rms_db=level, is_onset=is_onset)

        result = self.detector.detect(np.asarray(frame, dtype=np.float64))
        if not result.valid:
            return PitchEvent(t=t, hz=0.0, confidence=result.confidence, rms_db=level, is_onset=is_onset)

        hz = result.hz
        if self.detune_cents:
            hz = hz * (2.0 ** (-self.detune_cents / 1200.0))

        return PitchEvent(
            t=t,
            hz=hz,
            confidence=result.confidence,
            rms_db=level,
            is_onset=is_onset,
            harmonic_corrected=result.harmonic_corrected,
        )


@dataclass
class AnalyzerThread(threading.Thread):
    """从环形缓冲按帧移取窗并分析的后台线程。"""

    ring: RingBuffer
    analyzer: FrameAnalyzer
    on_event: Callable[[PitchEvent], None]
    config: AnalyzerConfig = field(default_factory=AnalyzerConfig)
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _t0: float | None = field(default=None, repr=False)
    processed: int = 0
    dropped: int = 0

    def __post_init__(self) -> None:
        super().__init__(name="JitaAnalyzer", daemon=True)

    def stop(self) -> None:
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def tick(self) -> PitchEvent | None:
        """取一窗并处理一次；数据不足时返回 None。供测试直接调用。"""
        frame = self.ring.read_latest(self.config.window)
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
        while not self._stop.is_set():
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
