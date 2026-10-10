"""会话设置与会话小结对话框。"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..core.instrument import StringMeasurer
from ..core.theory.levels import LEVELS, get_level
from ..core.theory.notes import midi_name
from ..practice.session import (
    COUNT_PRESETS,
    DURATION_PRESETS,
    MODE_COUNT,
    MODE_DURATION,
    MODE_FREE,
    TIMEOUT_AUTO,
    TIMEOUT_MANUAL,
    SessionConfig,
    SessionSummary,
)


class SessionSetupDialog(QDialog):
    """开始练习前的设置：难度、模式、时长/题量。"""

    def __init__(self, base: SessionConfig, tr, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tr = tr
        self.setWindowTitle(tr("practice.setup_title"))
        self.setMinimumWidth(460)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setSpacing(10)

        self.level_combo = QComboBox()
        for level in LEVELS:
            name = level.name_zh if tr.language == "zh_CN" else level.name_en
            self.level_combo.addItem(f"{name}（0–{level.max_fret} 品）", level.id)
        index = self.level_combo.findData(base.level_id)
        self.level_combo.setCurrentIndex(max(0, index))
        form.addRow(tr("practice.level"), self.level_combo)

        self.mode_combo = QComboBox()
        self.mode_combo.addItem(tr("practice.mode.count"), MODE_COUNT)
        self.mode_combo.addItem(tr("practice.mode.duration"), MODE_DURATION)
        self.mode_combo.addItem(tr("practice.mode.free"), MODE_FREE)
        mode_index = self.mode_combo.findData(base.mode)
        self.mode_combo.setCurrentIndex(max(0, mode_index))
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        form.addRow(tr("practice.mode"), self.mode_combo)

        self.target_combo = QComboBox()
        form.addRow(tr("practice.target"), self.target_combo)

        self.accidental_check = QCheckBox(tr("settings.include_accidentals"))
        self.accidental_check.setChecked(base.include_accidentals)
        form.addRow("", self.accidental_check)

        self.note_hint_check = QCheckBox(tr("settings.show_note_name"))
        self.note_hint_check.setChecked(base.show_note_name)
        form.addRow("", self.note_hint_check)

        # 超时处理：自动换题（显示倒计时）或人工推进
        self.timeout_combo = QComboBox()
        self.timeout_combo.addItem(tr("practice.timeout.auto"), TIMEOUT_AUTO)
        self.timeout_combo.addItem(tr("practice.timeout.manual"), TIMEOUT_MANUAL)
        timeout_index = self.timeout_combo.findData(base.timeout_mode)
        self.timeout_combo.setCurrentIndex(max(0, timeout_index))
        self.timeout_combo.currentIndexChanged.connect(self._on_timeout_changed)
        form.addRow(tr("practice.timeout"), self.timeout_combo)

        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(2, 60)
        self.timeout_spin.setSuffix(" " + tr("settings.seconds"))
        self.timeout_spin.setValue(max(2, int(round(base.timeout_seconds))))
        form.addRow(tr("practice.timeout_seconds"), self.timeout_spin)
        self._on_timeout_changed()

        # 宽容期：出题后允许试音而不判错的时长。0 = 立即判定。
        self.grace_spin = QDoubleSpinBox()
        self.grace_spin.setRange(0.0, 5.0)
        self.grace_spin.setSingleStep(0.5)
        self.grace_spin.setDecimals(1)
        self.grace_spin.setSuffix(" " + tr("settings.seconds"))
        self.grace_spin.setValue(max(0.0, min(5.0, base.grace_seconds)))
        self.grace_spin.setToolTip(tr("practice.grace_hint"))
        form.addRow(tr("practice.grace"), self.grace_spin)
        grace_hint = QLabel(tr("practice.grace_hint"))
        grace_hint.setObjectName("faint")
        grace_hint.setWordWrap(True)
        form.addRow("", grace_hint)

        self.scoring_check = QCheckBox(tr("settings.scoring"))
        self.scoring_check.setChecked(base.scoring_enabled)
        form.addRow("", self.scoring_check)

        layout.addLayout(form)

        hint = QLabel(tr("practice.setup_hint"))
        hint.setObjectName("faint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(tr("practice.start"))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr("common.cancel"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._base = base
        self._fill_targets(base)

    # ------------------------------------------------------------------ 内部
    def _fill_targets(self, base: SessionConfig) -> None:
        mode = self.mode_combo.currentData()
        self.target_combo.clear()
        if mode == MODE_DURATION:
            for minutes in DURATION_PRESETS:
                self.target_combo.addItem(f"{minutes} 分钟", minutes)
            index = self.target_combo.findData(base.target_minutes)
        elif mode == MODE_COUNT:
            for count in COUNT_PRESETS:
                self.target_combo.addItem(f"{count} 题", count)
            index = self.target_combo.findData(base.target_count)
        else:
            self.target_combo.addItem("不限", 0)
            index = 0
        self.target_combo.setCurrentIndex(max(0, index))
        self.target_combo.setEnabled(mode != MODE_FREE)

    def _on_mode_changed(self) -> None:
        self._fill_targets(self._base)

    def _on_timeout_changed(self) -> None:
        """只有自动模式需要设置超时秒数。"""
        auto = self.timeout_combo.currentData() == TIMEOUT_AUTO
        self.timeout_spin.setEnabled(auto)

    # ------------------------------------------------------------------ 结果
    def config(self) -> SessionConfig:
        mode = self.mode_combo.currentData()
        value = self.target_combo.currentData() or 0
        return SessionConfig(
            module_id=self._base.module_id,
            mode=mode,
            target_minutes=value if mode == MODE_DURATION else self._base.target_minutes,
            target_count=value if mode == MODE_COUNT else self._base.target_count,
            level_id=self.level_combo.currentData(),
            include_accidentals=self.accidental_check.isChecked(),
            scoring_enabled=self.scoring_check.isChecked(),
            show_note_name=self.note_hint_check.isChecked(),
            timeout_mode=self.timeout_combo.currentData(),
            timeout_seconds=float(self.timeout_spin.value()),
            grace_seconds=float(self.grace_spin.value()),
        )


class ProfileManagerDialog(QDialog):
    """账号与吉他档案管理。

    两个标签页：

    - **学习者账号**：新建 / 重命名 / 删除 / 设为当前。练习记录、记忆曲线、统计
      都按账号隔离，换账号等于换一个人练。
    - **吉他档案**：列出已保存的乐器配置档案，可测量新建、设为当前、重命名、删除、查看。

    对话框只负责"改了什么"，具体切换由主窗口执行（它要重载设置、首页与统计页）。
    """

    def __init__(
        self,
        settings: Settings,
        tr,
        *,
        current_profile_id: int | None = None,
        current_instrument_path: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.tr = tr
        self.current_profile_id = current_profile_id
        self.current_instrument_path = current_instrument_path
        #: 用户在本次对话框里做出的选择（未改动则为 None）
        self.selected_learner_id: int | None = None
        self.selected_instrument_path: str | None = None
        self._instrument_changed = False

        self.setWindowTitle(tr("accounts.title"))
        self.setMinimumSize(560, 420)
        self._build_ui()
        self.reload()

    # ------------------------------------------------------------------ 界面
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()

        # --- 学习者账号 ---
        learner = QWidget()
        learner_layout = QHBoxLayout(learner)
        self.learner_list = QListWidget()
        learner_layout.addWidget(self.learner_list, 1)
        learner_buttons = QVBoxLayout()
        self.new_learner_button = QPushButton(self.tr("accounts.new_learner"))
        self.new_learner_button.clicked.connect(self.create_learner)
        self.rename_learner_button = QPushButton(self.tr("accounts.rename"))
        self.rename_learner_button.clicked.connect(self.rename_learner)
        self.delete_learner_button = QPushButton(self.tr("accounts.delete"))
        self.delete_learner_button.clicked.connect(self.delete_learner)
        self.use_learner_button = QPushButton(self.tr("accounts.use"))
        self.use_learner_button.setObjectName("primary")
        self.use_learner_button.clicked.connect(self.use_learner)
        for button in (
            self.new_learner_button,
            self.rename_learner_button,
            self.delete_learner_button,
            self.use_learner_button,
        ):
            learner_buttons.addWidget(button)
        learner_buttons.addStretch(1)
        learner_layout.addLayout(learner_buttons)
        self.tabs.addTab(learner, self.tr("accounts.tab_learners"))

        # --- 吉他档案 ---
        instrument = QWidget()
        instrument_layout = QHBoxLayout(instrument)
        self.instrument_list = QListWidget()
        instrument_layout.addWidget(self.instrument_list, 1)
        instrument_buttons = QVBoxLayout()
        self.measure_button = QPushButton(self.tr("accounts.measure_new"))
        self.measure_button.clicked.connect(self.measure_new)
        self.use_instrument_button = QPushButton(self.tr("accounts.use"))
        self.use_instrument_button.setObjectName("primary")
        self.use_instrument_button.clicked.connect(self.use_instrument)
        self.rename_instrument_button = QPushButton(self.tr("accounts.rename"))
        self.rename_instrument_button.clicked.connect(self.rename_instrument)
        self.delete_instrument_button = QPushButton(self.tr("accounts.delete"))
        self.delete_instrument_button.clicked.connect(self.delete_instrument)
        self.show_instrument_button = QPushButton(self.tr("instrument.show"))
        self.show_instrument_button.clicked.connect(self.show_instrument)
        for button in (
            self.measure_button,
            self.use_instrument_button,
            self.rename_instrument_button,
            self.delete_instrument_button,
            self.show_instrument_button,
        ):
            instrument_buttons.addWidget(button)
        instrument_buttons.addStretch(1)
        instrument_layout.addLayout(instrument_buttons)
        self.tabs.addTab(instrument, self.tr("accounts.tab_instruments"))

        layout.addWidget(self.tabs)

        self.hint = QLabel(self.tr("accounts.hint"))
        self.hint.setObjectName("faint")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText(self.tr("common.close"))
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ------------------------------------------------------------------ 列表
    def reload(self) -> None:
        self._reload_learners()
        self._reload_instruments()

    def _reload_learners(self) -> None:
        from ..data.db import Database

        self.learner_list.clear()
        rows = Database.list_profiles(self.settings.conn)
        total = len(rows)
        for row in rows:
            profile_id = int(row["id"])
            name = str(row["name"])
            label = name
            if profile_id == (self.selected_learner_id or self.current_profile_id):
                label = f"● {name}"
            self.learner_list.addItem(label)
            item = self.learner_list.item(self.learner_list.count() - 1)
            item.setData(Qt.ItemDataRole.UserRole, profile_id)
            if profile_id == (self.selected_learner_id or self.current_profile_id):
                self.learner_list.setCurrentItem(item)
        # 只剩一个账号时不允许删除，避免把自己锁在外面
        self.delete_learner_button.setEnabled(total > 1)

    def _reload_instruments(self) -> None:
        from ..core.instrument import list_saved_profiles

        self.instrument_list.clear()
        active = self.selected_instrument_path or self.current_instrument_path
        for path, profile in list_saved_profiles():
            label = profile.name
            if profile.is_measured:
                label += "（已实测）"
            if active and str(path) == active:
                label = f"● {label}"
            self.instrument_list.addItem(label)
            item = self.instrument_list.item(self.instrument_list.count() - 1)
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            if active and str(path) == active:
                self.instrument_list.setCurrentItem(item)

    # ------------------------------------------------------------------ 学习者
    def create_learner(self) -> None:
        from ..data.db import Database

        name, ok = QInputDialog.getText(
            self, self.tr("accounts.new_learner"), self.tr("accounts.name_prompt")
        )
        if not ok or not name.strip():
            return
        Database.create_profile(self.settings.conn, name.strip())
        self._reload_learners()

    def rename_learner(self) -> None:
        from ..data.db import Database

        profile_id = self._current_learner_id()
        if profile_id is None:
            return
        row = self.settings.conn.execute(
            "SELECT name FROM profiles WHERE id = ?", (profile_id,)
        ).fetchone()
        old = str(row["name"]) if row else ""
        name, ok = QInputDialog.getText(
            self, self.tr("accounts.rename"), self.tr("accounts.name_prompt"), text=old
        )
        if not ok or not name.strip():
            return
        Database.rename_profile(self.settings.conn, profile_id, name.strip())
        self._reload_learners()

    def delete_learner(self) -> None:
        from ..data.db import Database

        profile_id = self._current_learner_id()
        if profile_id is None:
            return
        rows = Database.list_profiles(self.settings.conn)
        if len(rows) <= 1:
            QMessageBox.information(
                self, self.tr("accounts.delete"), self.tr("accounts.cannot_delete_last")
            )
            return
        answer = QMessageBox.question(
            self,
            self.tr("accounts.delete"),
            self.tr("accounts.confirm_delete_learner"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if profile_id == self.current_profile_id:
            # 删的是当前账号：自动切到剩下的第一个
            remaining = [int(row["id"]) for row in rows if int(row["id"]) != profile_id]
            self.selected_learner_id = remaining[0] if remaining else None
        Database.delete_profile(self.settings.conn, profile_id)
        self._reload_learners()

    def use_learner(self) -> None:
        profile_id = self._current_learner_id()
        if profile_id is None:
            return
        self.selected_learner_id = profile_id
        self._reload_learners()

    def _current_learner_id(self) -> int | None:
        item = self.learner_list.currentItem()
        if item is None:
            return None
        value = item.data(Qt.ItemDataRole.UserRole)
        return int(value) if value is not None else None

    # ------------------------------------------------------------------ 吉他
    def measure_new(self) -> None:
        """测量一把新吉他并保存为档案。"""
        dialog = MeasureGuitarDialog(self.settings, self.tr, self)
        dialog.exec()
        if dialog.saved_path is not None:
            self.selected_instrument_path = str(dialog.saved_path)
            self._instrument_changed = True
            self._reload_instruments()

    def use_instrument(self) -> None:
        path = self._current_instrument_path()
        if path is None:
            return
        self.selected_instrument_path = path
        self._instrument_changed = True
        self._reload_instruments()

    def rename_instrument(self) -> None:
        from ..core.instrument import ProfileError, rename_saved_profile

        path = self._current_instrument_path()
        if path is None:
            return
        from ..core.instrument import load_profile

        try:
            current = load_profile(path)
        except ProfileError as exc:
            QMessageBox.warning(self, self.tr("accounts.rename"), str(exc))
            return
        name, ok = QInputDialog.getText(
            self, self.tr("accounts.rename"), self.tr("accounts.name_prompt"), text=current.name
        )
        if not ok or not name.strip():
            return
        try:
            new_path = rename_saved_profile(path, name.strip())
        except ProfileError as exc:
            QMessageBox.warning(self, self.tr("accounts.rename"), str(exc))
            return
        if self.current_instrument_path == path or self.selected_instrument_path == path:
            self.selected_instrument_path = str(new_path)
            self._instrument_changed = True
        self._reload_instruments()

    def delete_instrument(self) -> None:
        from ..core.instrument import delete_saved_profile

        path = self._current_instrument_path()
        if path is None:
            return
        answer = QMessageBox.question(
            self,
            self.tr("accounts.delete"),
            self.tr("accounts.confirm_delete_instrument"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        delete_saved_profile(path)
        if self.current_instrument_path == path:
            # 删的是当前档案：回到内置标准档案
            self.selected_instrument_path = ""
            self._instrument_changed = True
        self._reload_instruments()

    def show_instrument(self) -> None:
        from ..core.instrument import ProfileError, load_profile, summarise

        path = self._current_instrument_path()
        if path is None:
            return
        try:
            profile = load_profile(path)
        except ProfileError as exc:
            QMessageBox.warning(self, self.tr("instrument.show"), str(exc))
            return
        lines = [summarise(profile), ""]
        for spec in profile.strings:
            measured = f"{spec.measured_hz:.2f} Hz" if spec.measured_hz else "—"
            cents = f"{spec.offset_cents:+.1f} 音分" if spec.offset_cents is not None else "—"
            lines.append(f"  第{spec.number}弦 {spec.note_name:>3}  实测 {measured:>10}  {cents}")
        lines.append("")
        lines.append(f"{self.tr('instrument.mode')}：{profile.detection.harmonic_mode}")
        if profile.notes:
            lines.append(profile.notes)
        QMessageBox.information(self, self.tr("instrument.show"), "\n".join(lines))

    def _current_instrument_path(self) -> str | None:
        item = self.instrument_list.currentItem()
        if item is None:
            return None
        value = item.data(Qt.ItemDataRole.UserRole)
        return str(value) if value else None

    @property
    def instrument_changed(self) -> bool:
        return self._instrument_changed


class MeasureGuitarDialog(QDialog):
    """界面内的"测量我的吉他"向导：逐根弦采集，生成配置档案。

    不需要命令行：依次拨响六根弦，程序用**与练习时相同**的分析链路测量，
    给出每根弦的音分偏差与提示，最后保存成一个配置档案并启用。

    目标音高来自当前档案（所以降半音的琴也能正确测量），偏差只作提示，
    不会把"没调准"固化成新调弦。
    """

    #: 每根弦的采集时长（秒）与开始前的稳定时间
    MEASURE_SECONDS = 3.0
    SETTLE_SECONDS = 0.4
    POLL_MS = 40

    def __init__(self, settings: Settings, tr, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from .audio_bridge import active_profile

        self.settings = settings
        self.tr = tr
        self.profile = active_profile(settings)
        self.order = [spec.number for spec in sorted(self.profile.strings, key=lambda s: -s.number)]
        self.targets = {spec.number: spec.midi for spec in self.profile.strings}
        self.measurers: dict[int, StringMeasurer] = {}
        self.index = 0
        self.audio = None
        self.result = None
        self.saved_path = None
        self._elapsed = 0.0
        self._finished = False
        self._timer = QTimer(self)
        self._timer.setInterval(self.POLL_MS)
        self._timer.timeout.connect(self._tick)

        self.setWindowTitle(tr("measure.title"))
        self.setMinimumWidth(520)
        self._build_ui()

    # ------------------------------------------------------------------ 界面
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        self.instructions = QLabel(self.tr("measure.instructions"))
        self.instructions.setWordWrap(True)
        layout.addWidget(self.instructions)

        self.prompt = QLabel(self.tr("measure.ready"))
        self.prompt.setObjectName("sectionTitle")
        self.prompt.setStyleSheet("font-size: 20px; font-weight: 600; padding: 10px 0;")
        layout.addWidget(self.prompt)

        self.table = QGridLayout()
        layout.addLayout(self.table)

        self.status = QLabel("")
        self.status.setObjectName("faint")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        buttons = QHBoxLayout()
        self.start_button = QPushButton(self.tr("measure.start"))
        self.start_button.setObjectName("primary")
        self.start_button.clicked.connect(self.start_measurement)
        self.save_button = QPushButton(self.tr("measure.save_and_use"))
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self.save_and_use)
        self.again_button = QPushButton(self.tr("measure.again"))
        self.again_button.setEnabled(False)
        self.again_button.clicked.connect(self.start_measurement)
        close_button = QPushButton(self.tr("common.close"))
        close_button.clicked.connect(self.reject)
        buttons.addStretch(1)
        buttons.addWidget(self.start_button)
        buttons.addWidget(self.again_button)
        buttons.addWidget(self.save_button)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)
        self._render_table()

    def _render_table(self) -> None:
        while self.table.count():
            item = self.table.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for row, number in enumerate(self.order):
            name = self.tr("tuner.string", n=number)
            label = QLabel(name)
            label.setObjectName("faint")
            measured = QLabel("—")
            measured.setObjectName("dim")
            self.table.addWidget(label, row, 0)
            self.table.addWidget(measured, row, 1)
            self.table.addWidget(QLabel(""), row, 2)
            setattr(self, f"_row_{number}", (measured, self.table.itemAt(row * 3 + 2).widget()))

    def _set_row(self, number: int, text: str, verdict: str = "") -> None:
        measured, verdict_label = getattr(self, f"_row_{number}")
        measured.setText(text)
        verdict_label.setText(verdict)

    # ------------------------------------------------------------------ 测量
    def start_measurement(self) -> None:
        from ..ui.audio_bridge import build_session

        self.measurers = {}
        self.index = 0
        self._elapsed = 0.0
        self._finished = False
        self.result = None
        self.saved_path = None
        self.save_button.setEnabled(False)
        self._render_table()
        self.status.setText("")

        if self.audio is not None:
            try:
                self.audio.stop()
            except Exception:  # noqa: BLE001
                pass
        try:
            self.audio = build_session(self.settings)
            self.audio.start()
        except Exception as exc:  # noqa: BLE001
            self.audio = None
            self.prompt.setText(self.tr("error.audio_unavailable", reason=str(exc)))
            return

        self._begin_string()
        self._timer.start()

    def _begin_string(self) -> None:
        number = self.order[self.index]
        self._measurer = StringMeasurer(number, self.targets[number])
        self.measurers[number] = self._measurer
        self._elapsed = 0.0
        self.prompt.setText(
            self.tr("measure.play_string", n=number, note=midi_name(self.targets[number]))
        )

    def _tick(self) -> None:
        if self._finished or self.audio is None:
            return
        for event in self.audio.poll():
            if event.valid and event.hz > 0:
                self._measurer.add(
                    event.hz, level_db=getattr(event, "rms_db", None), confidence=event.confidence
                )
        self._elapsed += self.POLL_MS / 1000.0
        if self._elapsed < self.SETTLE_SECONDS:
            return
        number = self.order[self.index]
        measured = self._measurer.median_hz
        if measured:
            self._set_row(number, f"{measured:.2f} Hz", self._measurer.verdict())
        if self._elapsed < self.SETTLE_SECONDS + self.MEASURE_SECONDS:
            return
        if not measured:
            self._set_row(number, "—", self.tr("measure.no_sound"))
        self.index += 1
        if self.index < len(self.order):
            self._begin_string()
        else:
            self._finish_measurement()

    def _finish_measurement(self) -> None:
        from ..core.instrument import (
            check_measurements,
            profile_from_measurements,
            summarise,
        )

        self._finished = True
        self._timer.stop()
        if self.audio is not None:
            try:
                self.audio.stop()
            except Exception:  # noqa: BLE001
                pass
            self.audio = None

        measurements = [
            result
            for result in (self.measurers[number].result() for number in self.order)
            if result is not None
        ]
        profile = profile_from_measurements(
            measurements,
            name=f"{self.profile.name}（实测）",
            tuning=[(spec.number, spec.midi) for spec in self.profile.strings],
            detection=self.profile.detection,
            max_fret=self.profile.max_fret,
            notes=self.tr("measure.notes"),
        )
        self.result = profile

        warnings = check_measurements(measurements, profile)
        self.prompt.setText(self.tr("measure.done", count=len(measurements)))
        lines = [summarise(profile)]
        for warning in warnings:
            icon = {"info": "ℹ", "warn": "⚠", "error": "❌"}[warning.severity]
            lines.append(f"{icon} {warning.message}")
            if warning.advice:
                lines.append(f"    → {warning.advice}")
        if not warnings:
            lines.append(self.tr("measure.no_problem"))
        self.status.setText("\n".join(lines))
        self.save_button.setEnabled(True)
        self.again_button.setEnabled(True)

    def save_and_use(self) -> None:
        """保存档案并设为当前档案。"""
        from ... import paths
        from ..core.instrument import save_profile

        if self.result is None:
            return
        target_dir = paths.data_dir() / "instruments"
        target_dir.mkdir(parents=True, exist_ok=True)
        path = save_profile(self.result, target_dir / f"{self.result.id}.json")
        self.settings.set("instrument_profile", str(path))
        self.saved_path = path
        self.status.setText(self.tr("measure.saved", path=path.name))
        self.save_button.setEnabled(False)

    def closeEvent(self, event) -> None:  # noqa: ANN001, N802
        """关窗前一定要停掉采集，别占着麦克风。"""
        self._timer.stop()
        if self.audio is not None:
            try:
                self.audio.stop()
            except Exception:  # noqa: BLE001
                pass
            self.audio = None
        super().closeEvent(event)


class SessionSummaryDialog(QDialog):
    """会话小结（需求 FR-820）。"""

    def __init__(self, summary: SessionSummary, tr, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tr = tr
        self.setWindowTitle(tr("stats.session_summary"))
        self.setMinimumWidth(420)
        self.retry_requested = False

        layout = QVBoxLayout(self)
        heading = QLabel(tr("stats.session_summary"))
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)

        level = get_level(summary.level_id)
        level_name = level.name_zh if tr.language == "zh_CN" else level.name_en
        layout.addWidget(QLabel(f"{tr('practice.level')}：{level_name}（0–{level.max_fret} 品）"))

        box = QGroupBox(tr("stats.title"))
        grid = QGridLayout(box)
        rows = [
            (tr("practice.progress", answered=summary.asked, total=summary.asked), ""),
            (tr("stats.accuracy"), f"{summary.accuracy_first * 100:.0f}%"),
            (tr("stats.avg_rt"), f"{summary.avg_rt_ms} ms" if summary.avg_rt_ms else "—"),
            (tr("stats.best_combo"), str(summary.best_combo)),
            (tr("practice.score"), str(summary.score)),
            (tr("practice.timeout"), str(summary.timeouts)),
            (tr("practice.skip"), str(summary.skipped)),
        ]
        for row, (key, value) in enumerate(rows):
            key_label = QLabel(key)
            key_label.setObjectName("faint")
            grid.addWidget(key_label, row, 0)
            grid.addWidget(QLabel(value), row, 1)
        layout.addWidget(box)

        if summary.weakest:
            weak = QLabel(
                tr("stats.weakest")
                + "："
                + "、".join(f"{key.split('|')[0].split('=')[1]}（{count} 次）" for key, count in summary.weakest)
            )
            weak.setWordWrap(True)
            weak.setObjectName("dim")
            layout.addWidget(weak)

        buttons = QHBoxLayout()
        again = QPushButton(tr("practice.again"))
        again.setObjectName("primary")
        again.clicked.connect(self._on_again)
        close = QPushButton(tr("common.close"))
        close.clicked.connect(self.reject)
        buttons.addStretch(1)
        buttons.addWidget(again)
        buttons.addWidget(close)
        layout.addLayout(buttons)

    def _on_again(self) -> None:
        self.retry_requested = True
        self.accept()


__all__ = ["SessionSetupDialog", "SessionSummaryDialog"]
