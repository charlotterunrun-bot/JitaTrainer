"""音频会话：把采集、分析、事件分发打包成一个可启动/停止的单元。

UI 侧不需要了解线程细节：``start()`` 之后用定时器 ``poll()`` 取事件即可。
这样设计也让"向导测噪声""调音器""练习屏"共用同一条音频链路。
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass

from .analyzer import AnalyzerConfig, AnalyzerThread, FrameAnalyzer
from .capture import AudioCapture, CaptureConfig
from .events import PitchEvent
from .gate import GateConfig
from .ring import RingBuffer

RING_SECONDS = 0.5


@dataclass
class SessionStats:
    events: int = 0
    voiced: int = 0
    onsets: int = 0


class AudioSession:
    """采集 + 分析 + 事件队列。"""

    def __init__(
        self,
        *,
        samplerate: int = 48000,
        gate: GateConfig | None = None,
        capture_config: CaptureConfig | None = None,
        analyzer_config: AnalyzerConfig | None = None,
        queue_size: int = 256,
    ) -> None:
        self.samplerate = samplerate
        self.gate = gate or GateConfig()
        self.analyzer_config = analyzer_config or AnalyzerConfig(samplerate=samplerate)
        self.capture_config = capture_config or CaptureConfig(samplerate=samplerate)

        capacity = int(samplerate * RING_SECONDS)
        self.ring = RingBuffer(capacity=capacity, channels=1)
        self.capture = AudioCapture(self.ring, self.capture_config, on_error=self._on_capture_error)
        self.analyzer = FrameAnalyzer(self.analyzer_config, self.gate)
        self._queue: deque[PitchEvent] = deque(maxlen=queue_size)
        self._lock = threading.Lock()
        self._thread: AnalyzerThread | None = None
        self.stats = SessionStats()
        self.last_error: str | None = None

    # ------------------------------------------------------------------ 生命周期
    def _on_capture_error(self, message: str) -> None:
        self.last_error = message

    def _on_event(self, event: PitchEvent) -> None:
        with self._lock:
            self._queue.append(event)

    @property
    def is_running(self) -> bool:
        return self._thread is not None and not self._thread.stopped

    def set_gate(self, gate: GateConfig) -> None:
        self.gate = gate
        self.analyzer.update_gate(gate)

    def set_detune_cents(self, cents: float) -> None:
        self.analyzer.set_detune_cents(cents)

    def update_capture_device(self, device_index: int | None, samplerate: int | None = None) -> None:
        """切换设备（需先 stop）。"""
        self.capture_config = CaptureConfig(
            device_index=device_index,
            samplerate=samplerate or self.capture_config.samplerate,
            blocksize=self.capture_config.blocksize,
        )

    def start(self) -> None:
        if self.is_running:
            return
        self.ring.clear()
        with self._lock:
            self._queue.clear()
        self.capture.start()
        thread = AnalyzerThread(
            ring=self.ring,
            analyzer=self.analyzer,
            on_event=self._on_event,
            config=self.analyzer_config,
        )
        thread.start()
        self._thread = thread

    def stop(self) -> None:
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.stop()
            thread.join(timeout=2.0)
            self.stats.events += thread.processed
        self.capture.stop()

    # ------------------------------------------------------------------ 事件
    def poll(self) -> list[PitchEvent]:
        """取出自上次调用以来累积的事件（非阻塞）。"""
        with self._lock:
            events = list(self._queue)
            self._queue.clear()
        for event in events:
            if event.valid:
                self.stats.voiced += 1
            if event.is_onset:
                self.stats.onsets += 1
        return events

    def latest_voiced(self) -> PitchEvent | None:
        """最近一个有效音高事件（用于调音器显示）。"""
        with self._lock:
            for event in reversed(self._queue):
                if event.valid:
                    return event
        return None
