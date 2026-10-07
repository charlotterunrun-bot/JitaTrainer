"""界面与音频层之间的桥接工具。"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from ..core.audio.capture import CaptureConfig
from ..core.audio.gate import GateConfig
from ..core.audio.session import AudioSession
from ..data.settings import Settings


def build_session(settings: Settings) -> AudioSession:
    """按当前设置构造音频会话。"""
    samplerate = settings.get_int("samplerate", 48000)
    gate = GateConfig(
        noise_floor_db=settings.get_float("noise_floor_db", -60.0),
        offset_db=settings.get_float("gate_offset_db", 12.0),
    )
    capture_config = CaptureConfig(
        device_index=settings.get_optional_int("input_device_index"),
        samplerate=samplerate,
    )
    session = AudioSession(samplerate=samplerate, gate=gate, capture_config=capture_config)
    session.set_detune_cents(settings.get_float("calibration_offset_cents", 0.0))
    return session


class BackgroundTask:
    """把阻塞式调用放到后台线程，用定时器轮询结果（避免 UI 卡死）。

    不直接使用 Qt 线程，是为了让被测函数保持纯 Python、可单元测试。
    """

    def __init__(
        self,
        work: Callable[[], Any],
        on_done: Callable[[Any], None],
        on_error: Callable[[BaseException], None] | None = None,
    ) -> None:
        self._work = work
        self._on_done = on_done
        self._on_error = on_error
        self._result: Any = None
        self._error: BaseException | None = None
        self._finished = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        def runner() -> None:
            try:
                self._result = self._work()
            except BaseException as exc:  # noqa: BLE001 - 需要把异常带回 UI 线程
                self._error = exc
            finally:
                self._finished.set()

        self._thread = threading.Thread(target=runner, name="JitaTask", daemon=True)
        self._thread.start()

    @property
    def finished(self) -> bool:
        return self._finished.is_set()

    def poll(self) -> bool:
        """已结束时回调并返回 True；未结束返回 False。只会回调一次。"""
        if not self._finished.is_set():
            return False
        self._finished.clear()  # 防止重复回调
        if self._error is not None:
            if self._on_error is not None:
                self._on_error(self._error)
        else:
            self._on_done(self._result)
        return True
