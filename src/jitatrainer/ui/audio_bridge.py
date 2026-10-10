"""界面与音频层之间的桥接工具。"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from ..core.audio.capture import CaptureConfig
from ..core.audio.gate import GateConfig
from ..core.audio.session import AudioSession
from ..core.instrument import load_profile, standard_profile
from ..data.settings import Settings


def active_profile(settings: Settings):  # noqa: ANN201 - InstrumentProfile
    """读取当前乐器配置档案；没配置或读失败时退回内置标准档案。

    读失败不抛异常：配置坏了不该让程序起不来，退回默认值继续可用。
    """
    path = settings.get("instrument_profile", "")
    if not path:
        return standard_profile()
    try:
        from pathlib import Path

        candidate = Path(path)
        if not candidate.is_file():
            return standard_profile()
        return load_profile(candidate)
    except Exception:  # noqa: BLE001
        return standard_profile()


def build_session(settings: Settings) -> AudioSession:
    """按当前设置 + 乐器配置档案构造音频会话。

    检测策略（谐波校验模式、低频增强、频率范围）来自**配置档案**而不是写死的常量：
    不同吉他/麦克风在这几项上的最优取值不同，2026-10-10 的实测已经证明
    写死的策略会在一部分设备上把正确读数改错。
    """
    samplerate = settings.get_int("samplerate", 48000)
    gate = GateConfig(
        noise_floor_db=settings.get_float("noise_floor_db", -60.0),
        offset_db=settings.get_float("gate_offset_db", 12.0),
    )
    capture_config = CaptureConfig(
        device_index=settings.get_optional_int("input_device_index"),
        samplerate=samplerate,
    )

    profile = active_profile(settings)
    from ..core.audio.analyzer import AnalyzerConfig

    analyzer_config = AnalyzerConfig(
        samplerate=samplerate,
        low_enhance=profile.detection.low_enhance,
        low_band_hz=profile.detection.low_band_hz,
        harmonic_mode=profile.detection.harmonic_mode,
    )
    session = AudioSession(
        samplerate=samplerate,
        gate=gate,
        capture_config=capture_config,
        analyzer_config=analyzer_config,
    )
    session.set_detune_cents(settings.get_float("calibration_offset_cents", 0.0))
    session.profile = profile  # type: ignore[attr-defined]
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
