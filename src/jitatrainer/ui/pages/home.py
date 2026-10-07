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
    """首页：练习模块入口 + 调音器 + 向导。"""

    tuner_requested = Signal()
    wizard_requested = Signal()
    practice_requested = Signal(str)  # 模块 id
    stats_requested = Signal()

    def __init__(self, settings: Settings, tr, profile_name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.tr = tr
        self.profile_name = profile_name
        self._build_ui()

    def _build_ui(self) -> None:
        from ...practice import registry

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

        # 练习模块卡片（新增模块只要注册，首页自动出现）
        module_row = QHBoxLayout()
        module_row.setSpacing(14)
        for module in registry.all_modules():
            card = self._make_card(
                module.label(self.tr.language),
                module.describe(self.tr.language),
                self.tr("practice.start"),
                lambda _checked=False, mid=module.id: self.practice_requested.emit(mid),
            )
            module_row.addWidget(card, 1)
        module_row.addStretch(1)
        layout.addLayout(module_row)

        cards = QHBoxLayout()
        cards.setSpacing(14)
        cards.addWidget(
            self._make_card(
                self.tr("stats.title"),
                "正确率曲线、反应时间、错音热力图，并可导出 CSV"
                if self.tr.language == "zh_CN"
                else "Accuracy trend, reaction time, error heatmap, CSV export",
                self.tr("common.start"),
                self.stats_requested.emit,
            ),
            1,
        )
        cards.addWidget(
            self._make_card(
                self.tr("tuner.title"),
                "用麦克风检测六根弦的音高，指针式显示偏差" if self.tr.language == "zh_CN" else "Detect each string and show the cents offset",
                self.tr("common.start"),
                self.tuner_requested.emit,
            ),
            1,
        )
        cards.addWidget(
            self._make_card(
                self.tr("wizard.title"),
                "选择麦克风、测量环境噪声、试弹校准" if self.tr.language == "zh_CN" else "Pick a microphone, measure noise, calibrate by playing",
                self.tr("common.start"),
                self.wizard_requested.emit,
            ),
            1,
        )
        cards.addStretch(1)
        layout.addLayout(cards)

        self.info_grid = QGridLayout()
        self.info_grid.setHorizontalSpacing(18)
        self.info_grid.setVerticalSpacing(8)
        layout.addLayout(self.info_grid)

        self.advice_label = QLabel("")
        self.advice_label.setStyleSheet(
            "color: #e8b339; font-size: 15px; font-weight: 600; padding: 6px 0;"
        )
        self.advice_label.setVisible(False)
        layout.addWidget(self.advice_label)
        layout.addStretch(1)

        self.hint = QLabel("M3 进行中：记忆曲线调度已接入，统计报告将在 M4 实现。")
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
        """刷新信息区（设备、门限、档案、版本、到期复习、难度建议）。"""
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

        due_text, advice_text = self._learning_status()
        rows = [
            (self.tr("home.profiles"), self.profile_name),
            (self.tr("settings.input_device"), device_name),
            (self.tr("settings.stable_preset"), self._preset_label()),
            ("起音门限", f"{self.settings.get_float('noise_floor_db', -60.0) + self.settings.get_float('gate_offset_db', 12.0):.1f} dB"),
            (self.tr("home.review_status"), due_text),
            (self.tr("app.version"), __version__),
        ]
        for row, (key, value) in enumerate(rows):
            key_label = QLabel(key)
            key_label.setObjectName("faint")
            value_label = QLabel(str(value))
            self.info_grid.addWidget(key_label, row, 0)
            self.info_grid.addWidget(value_label, row, 1)

        self.advice_label.setText(advice_text)
        self.advice_label.setVisible(bool(advice_text))

    def _learning_status(self) -> tuple[str, str]:
        """到期复习数量与难度进阶建议（M3）。"""
        from ...core.theory.levels import next_level
        from ...data.repository import PracticeStatsRepository

        level_id = self.settings.get("level_id", "L1")
        try:
            repository = PracticeStatsRepository(self.settings.db, self.profile_id or 1)
            conn = self.settings.conn
            due = repository.due_count(conn, level_id)
            advice = repository.should_advance_level(conn, level_id)
        except Exception:  # noqa: BLE001 - 统计失败不影响首页
            return "—", ""

        due_text = self.tr("home.due_today", count=due) if due else self.tr("home.no_due")
        advice_text = ""
        if advice:
            upcoming = next_level(level_id)
            if upcoming is not None:
                name = upcoming.name_zh if self.tr.language == "zh_CN" else upcoming.name_en
                advice_text = self.tr(
                    "home.suggest_unlock", level=f"{name}（0–{upcoming.max_fret} 品）"
                )
        return due_text, advice_text

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
