"""首次运行向导：选设备 → 测环境噪声 → 试弹校准（需求 FR-300）。

三步都可能卡住（没有麦克风、环境太吵、琴没调准），所以每一步都可跳过，
且失败时给出明确原因而不是静默继续。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWizard,
    QWizardPage,
    QWidget,
)

from ...core.audio import device as device_mod
from ...core.audio.capture import CaptureConfig, measure_noise_floor
from ...core.audio.gate import GateConfig, NoiseFloorMeter
from ...core.audio.session import AudioSession
from ...core.theory.notes import cents_between, midi_name
from ...core.theory.tuning import STANDARD
from ...data.settings import Settings
from ..audio_bridge import BackgroundTask, build_session

MEASURE_SECONDS = 3.0
CALIBRATION_STRINGS = (6, 5, 4, 3, 2, 1)
CALIBRATION_TOLERANCE_CENTS = 60.0


@dataclass
class WizardResult:
    """向导产出，由主窗口写回设置。"""

    device_index: int | None = None
    samplerate: int = 48000
    noise_floor_db: float = -60.0
    gate_offset_db: float = 12.0
    calibration_offset_cents: float = 0.0
    calibrated_strings: list[int] = field(default_factory=list)
    completed: bool = False


class DevicePage(QWizardPage):
    def __init__(self, tr, result: WizardResult) -> None:
        super().__init__()
        self.tr = tr
        self.result = result
        self.setTitle(tr("wizard.device.title"))
        self.setSubTitle(tr("wizard.device.desc"))

        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        self.combo = QComboBox()
        self.refresh_button = QPushButton("↻")
        self.refresh_button.setFixedWidth(40)
        self.refresh_button.clicked.connect(self.reload)
        row.addWidget(self.combo, 1)
        row.addWidget(self.refresh_button)
        layout.addLayout(row)

        self.info = QLabel("")
        self.info.setObjectName("faint")
        self.info.setWordWrap(True)
        layout.addWidget(self.info)
        layout.addStretch(1)

        self.reload()

    def reload(self) -> None:
        self.combo.clear()
        devices = device_mod.list_input_devices()
        if not devices:
            self.info.setText(self.tr("error.no_input_device"))
            return
        for item in devices:
            label = f"{item.name}  [{item.max_input_channels}ch @ {int(item.default_samplerate)}Hz]"
            if item.is_default:
                label = "★ " + label
            self.combo.addItem(label, item.index)
        self.combo.setCurrentIndex(0)
        self.info.setText(f"共 {len(devices)} 个输入设备")

    def validatePage(self) -> bool:  # noqa: N802
        index = self.combo.currentData()
        if isinstance(index, int):
            self.result.device_index = index
            for item in device_mod.list_input_devices():
                if item.index == index:
                    self.result.samplerate = device_mod.negotiate_samplerate(item, 48000)
                    break
        return True


class NoisePage(QWizardPage):
    def __init__(self, tr, result: WizardResult, settings: Settings) -> None:
        super().__init__()
        self.tr = tr
        self.result = result
        self.settings = settings
        self.meter: NoiseFloorMeter | None = None
        self.task: BackgroundTask | None = None

        self.setTitle(tr("wizard.noise.title"))
        self.setSubTitle(tr("wizard.noise.desc", seconds=int(MEASURE_SECONDS)))

        layout = QVBoxLayout(self)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        self.status = QLabel(tr("wizard.noise.measuring"))
        self.status.setObjectName("dim")
        layout.addWidget(self.status)

        row = QHBoxLayout()
        self.start_button = QPushButton(tr("common.start"))
        self.start_button.setObjectName("primary")
        self.start_button.clicked.connect(self.start_measure)
        row.addWidget(self.start_button)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addStretch(1)

        self.timer = QTimer(self)
        self.timer.setInterval(120)
        self.timer.timeout.connect(self._poll)

    def start_measure(self) -> None:
        self.start_button.setEnabled(False)
        self.status.setText(self.tr("wizard.noise.measuring"))
        self.progress.setValue(0)

        device_index = self.result.device_index
        samplerate = self.result.samplerate

        def work() -> NoiseFloorMeter:
            return measure_noise_floor(device_index, samplerate=samplerate, seconds=MEASURE_SECONDS)

        self.task = BackgroundTask(work, self._done, self._failed)
        self.task.start()
        self.timer.start()

    def _poll(self) -> None:
        if self.task is None:
            return
        value = self.progress.value() + 4
        self.progress.setValue(min(96, value))
        if self.task.poll():
            self.timer.stop()
            self.start_button.setEnabled(True)

    def _done(self, meter: NoiseFloorMeter) -> None:
        self.progress.setValue(100)
        self.result.noise_floor_db = meter.floor_db
        gate = meter.suggest_gate(self.result.gate_offset_db)
        self.status.setText(
            self.tr("wizard.noise.result", floor=f"{meter.floor_db:.1f}", gate=f"{gate:.1f}")
        )
        self.completeChanged.emit()

    def _failed(self, exc: BaseException) -> None:
        self.progress.setValue(0)
        self.status.setText(self.tr("error.audio_unavailable", reason=str(exc)))
        self.start_button.setEnabled(True)
        self.completeChanged.emit()


class CalibratePage(QWizardPage):
    """试弹校准：依次拨响六根空弦，测出整体检测偏差。"""

    def __init__(self, tr, result: WizardResult, settings: Settings) -> None:
        super().__init__()
        self.tr = tr
        self.result = result
        self.settings = settings
        self.session: AudioSession | None = None
        self.index = 0
        self.deviations: list[float] = []

        self.setTitle(tr("wizard.calibrate.title"))
        self.setSubTitle(tr("wizard.calibrate.desc", string=midi_name(STANDARD.open_midi(6))))

        layout = QVBoxLayout(self)
        self.target_label = QLabel("")
        self.target_label.setObjectName("bigNote")
        layout.addWidget(self.target_label)

        self.detail = QLabel("")
        self.detail.setObjectName("dim")
        layout.addWidget(self.detail)

        self.progress = QProgressBar()
        self.progress.setRange(0, len(CALIBRATION_STRINGS))
        layout.addWidget(self.progress)
        layout.addStretch(1)

        self.timer = QTimer(self)
        self.timer.setInterval(40)
        self.timer.timeout.connect(self._poll)
        self._update_target()

    # ------------------------------------------------------------------ 流程
    def initializePage(self) -> None:  # noqa: N802
        self.index = 0
        self.deviations = []
        self.progress.setValue(0)
        self._update_target()
        if self.session is None:
            try:
                self.session = build_session(self.settings)
                self.session.update_capture_device(self.result.device_index, self.result.samplerate)
                self.session.set_gate(
                    GateConfig(noise_floor_db=self.result.noise_floor_db, offset_db=self.result.gate_offset_db)
                )
                self.session.start()
                self.timer.start()
                self.detail.setText(self.tr("wizard.calibrate.desc", string=midi_name(STANDARD.open_midi(6))))
            except Exception as exc:  # noqa: BLE001
                self.detail.setText(self.tr("error.audio_unavailable", reason=str(exc)))

    def cleanupPage(self) -> None:  # noqa: N802
        self._stop()

    def _stop(self) -> None:
        self.timer.stop()
        if self.session is not None:
            self.session.stop()
            self.session = None

    def _current_string(self) -> int | None:
        if self.index >= len(CALIBRATION_STRINGS):
            return None
        return CALIBRATION_STRINGS[self.index]

    def _update_target(self) -> None:
        string_no = self._current_string()
        if string_no is None:
            self.target_label.setText("✓")
            self.progress.setValue(len(CALIBRATION_STRINGS))
            return
        target = STANDARD.open_midi(string_no)
        self.target_label.setText(f"{self.tr('tuner.string', n=string_no)}  {midi_name(target)}")
        self.progress.setValue(self.index)

    def _poll(self) -> None:
        if self.session is None:
            return
        string_no = self._current_string()
        if string_no is None:
            return
        target_midi = STANDARD.open_midi(string_no)
        events = [e for e in self.session.poll() if e.valid]
        if not events:
            return

        latest = events[-1]
        cents = 100.0 * (latest.midi - target_midi)
        if latest.pitch_class != target_midi % 12:
            self.detail.setText(
                f"听到 {midi_name(round(latest.midi))}，需要 {midi_name(target_midi)}"
            )
            return
        if abs(cents) > CALIBRATION_TOLERANCE_CENTS * 2:
            # 音名对但差得太多，可能是别的八度，继续等
            return

        self.deviations.append(cents)
        self.result.calibrated_strings.append(string_no)
        self.index += 1
        self._update_target()

        if self.index >= len(CALIBRATION_STRINGS):
            self._finish()
        else:
            self.detail.setText(f"已识别 {len(self.deviations)}/6")

    def _finish(self) -> None:
        self._stop()
        if self.deviations:
            mean = sum(self.deviations) / len(self.deviations)
            self.result.calibration_offset_cents = round(mean, 1)
            self.detail.setText(
                self.tr("wizard.calibrate.done", count=len(self.deviations))
                + f"　平均偏差 {mean:+.1f} 音分"
            )
        else:
            self.detail.setText(self.tr("wizard.calibrate.failed"))
        self.completeChanged.emit()


class FirstRunWizard(QWizard):
    """三步向导。"""

    def __init__(self, settings: Settings, tr, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.tr = tr
        self.result = WizardResult(
            device_index=settings.get_optional_int("input_device_index"),
            samplerate=settings.get_int("samplerate", 48000),
            noise_floor_db=settings.get_float("noise_floor_db", -60.0),
            gate_offset_db=settings.get_float("gate_offset_db", 12.0),
        )

        self.setWindowTitle(tr("wizard.title"))
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.setMinimumSize(620, 420)
        self.setButtonText(QWizard.WizardButton.NextButton, tr("common.next"))
        self.setButtonText(QWizard.WizardButton.BackButton, tr("common.back"))
        self.setButtonText(QWizard.WizardButton.FinishButton, tr("common.finish"))
        self.setButtonText(QWizard.WizardButton.CancelButton, tr("common.skip"))

        self.device_page = DevicePage(tr, self.result)
        self.noise_page = NoisePage(tr, self.result, settings)
        self.calibrate_page = CalibratePage(tr, self.result, settings)
        self.addPage(self.device_page)
        self.addPage(self.noise_page)
        self.addPage(self.calibrate_page)

    def apply(self) -> None:
        """把向导结果写回设置。"""
        self.result.completed = True
        values = {
            "input_device_index": self.result.device_index if self.result.device_index is not None else "",
            "samplerate": self.result.samplerate,
            "noise_floor_db": round(self.result.noise_floor_db, 2),
            "gate_offset_db": self.result.gate_offset_db,
            "calibration_offset_cents": self.result.calibration_offset_cents,
            "wizard_completed": True,
        }
        self.settings.update(values)
