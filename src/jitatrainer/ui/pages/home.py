"""首页：档案信息、模块入口、设置入口。"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ... import __version__
from ...core.audio import device as device_mod
from ...data.settings import Settings


class HomePage(QWidget):
    """M1 版首页。M2 会在这里挂上练习模块卡片。"""

    tuner_requested = Signal()
    wizard_requested = Signal()

    def __init__(self, settings: Settings, tr, profile_name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.tr = tr
        self.profile_name = profile_name
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)

        title = QLabel(self.tr("app.title"))
        title.setObjectName("title")
        subtitle = QLabel(self.tr("app.subtitle"))
        subtitle.setObjectName("subtitle")
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addSpacing(8)

        cards = QHBoxLayout()
        cards.setSpacing(14)

        tuner_card = self._make_card(
            self.tr("tuner.title"),
            "用麦克风检测六根弦的音高，指针式显示偏差",
            self.tr("common.start"),
            self.tuner_requested.emit,
        )
        wizard_card = self._make_card(
            self.tr("wizard.title"),
            "选择麦克风、测量环境噪声、试弹校准",
            self.tr("common.start"),
            self.wizard_requested.emit,
        )
        cards.addWidget(tuner_card, 1)
        cards.addWidget(wizard_card, 1)
        layout.addLayout(cards)

        self.info_grid = QGridLayout()
        self.info_grid.setHorizontalSpacing(18)
        self.info_grid.setVerticalSpacing(8)
        layout.addLayout(self.info_grid)
        layout.addStretch(1)

        self.hint = QLabel("M1 地基阶段：练习模块将在 M2 里程碑接入。")
        self.hint.setObjectName("faint")
        layout.addWidget(self.hint)

    def _make_card(self, title: str, desc: str, button_text: str, on_click) -> QFrame:  # noqa: ANN001
        frame = QFrame()
        frame.setStyleSheet("QFrame { background-color: #1c2229; border: 1px solid #2c343d; border-radius: 10px; }")
        box = QVBoxLayout(frame)
        box.setContentsMargins(18, 16, 18, 16)
        box.setSpacing(8)

        heading = QLabel(title)
        heading.setObjectName("sectionTitle")
        body = QLabel(desc)
        body.setObjectName("dim")
        body.setWordWrap(True)
        button = QPushButton(button_text)
        button.setObjectName("primary")
        button.clicked.connect(on_click)

        box.addWidget(heading)
        box.addWidget(body)
        box.addStretch(1)
        box.addWidget(button)
        return frame

    def refresh(self) -> None:
        """刷新信息区（设备、门限、档案、版本）。"""
        while self.info_grid.count():
            item = self.info_grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        device_index = self.settings.get_optional_int("input_device_index")
        device_name = "（未选择，使用系统默认）"
        for item in device_mod.list_input_devices():
            if item.index == device_index:
                device_name = item.name
                break
        if device_index is None:
            default = device_mod.default_input_device()
            if default is not None:
                device_name = f"{default.name}（系统默认）"

        rows = [
            (self.tr("home.profiles"), self.profile_name),
            (self.tr("settings.input_device"), device_name),
            (self.tr("settings.stable_preset"), self._preset_label()),
            ("起音门限", f"{self.settings.get_float('noise_floor_db', -60.0) + self.settings.get_float('gate_offset_db', 12.0):.1f} dB"),
            (self.tr("app.version"), __version__),
        ]
        for row, (key, value) in enumerate(rows):
            key_label = QLabel(key)
            key_label.setObjectName("faint")
            value_label = QLabel(str(value))
            self.info_grid.addWidget(key_label, row, 0)
            self.info_grid.addWidget(value_label, row, 1)

    def _preset_label(self) -> str:
        preset = self.settings.get("stable_preset", "balanced")
        from ...core.judge.base import PRESET_LATENCY_MS, STABLE_PRESETS

        ms = STABLE_PRESETS.get(preset, 200)
        latency = PRESET_LATENCY_MS.get(preset, 0)
        name = {
            "fast": self.tr("settings.stable.fast"),
            "balanced": self.tr("settings.stable.balanced"),
            "robust": self.tr("settings.stable.robust"),
        }.get(preset, preset)
        return f"{name}（{ms}ms，约 {latency}ms 响应）"
