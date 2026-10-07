"""音频设备枚举。

对 sounddevice 采取**延迟导入**：没有安装 sounddevice（或没有音频设备）时，
程序其余部分仍然可用（例如只做数据与统计），只是无法练习。
"""

from __future__ import annotations

from dataclasses import dataclass

_sd = None
_import_error: str | None = None


def _sounddevice():
    """延迟导入 sounddevice；失败时返回 None 并记录原因。"""
    global _sd, _import_error
    if _sd is not None or _import_error is not None:
        return _sd
    try:
        import sounddevice  # noqa: PLC0415

        _sd = sounddevice
    except Exception as exc:  # noqa: BLE001
        _import_error = f"{type(exc).__name__}: {exc}"
    return _sd


def sounddevice_available() -> bool:
    return _sounddevice() is not None


def import_error() -> str | None:
    return _import_error


@dataclass(frozen=True, slots=True)
class InputDevice:
    index: int
    name: str
    max_input_channels: int
    default_samplerate: float
    is_default: bool = False
    hostapi: str = ""


def _hostapi_names(sd) -> dict[int, str]:
    try:
        return {i: api["name"] for i, api in enumerate(sd.query_hostapis())}
    except Exception:  # noqa: BLE001
        return {}


def list_input_devices() -> list[InputDevice]:
    """列出全部输入设备（按默认设备优先排序）。"""
    sd = _sounddevice()
    if sd is None:
        return []

    try:
        default_index = sd.default.device[0]
    except Exception:  # noqa: BLE001
        default_index = -1

    apis = _hostapi_names(sd)
    devices: list[InputDevice] = []
    try:
        raw = sd.query_devices()
    except Exception:  # noqa: BLE001
        return []

    for index, info in enumerate(raw):
        if info.get("max_input_channels", 0) <= 0:
            continue
        devices.append(
            InputDevice(
                index=index,
                name=str(info.get("name", f"设备 {index}")),
                max_input_channels=int(info.get("max_input_channels", 0)),
                default_samplerate=float(info.get("default_samplerate", 0.0)),
                is_default=index == default_index,
                hostapi=apis.get(int(info.get("hostapi", -1)), ""),
            )
        )
    devices.sort(key=lambda d: (not d.is_default, d.index))
    return devices


def default_input_device() -> InputDevice | None:
    devices = list_input_devices()
    for device in devices:
        if device.is_default:
            return device
    return devices[0] if devices else None


def negotiate_samplerate(device: InputDevice, preferred: int = 48000) -> int:
    """协商可用采样率：优先 48kHz，否则回退设备的默认采样率。"""
    sd = _sounddevice()
    if sd is None:
        return preferred
    try:
        sd.check_input_settings(device=device.index, samplerate=preferred, channels=1, dtype="float32")
        return preferred
    except Exception:  # noqa: BLE001
        fallback = int(device.default_samplerate) or 44100
        return fallback
