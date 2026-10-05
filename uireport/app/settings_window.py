"""Settings window. Owner: output/app builder (package C)."""
from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..hotkeyspec import (
    MOD_ALT,
    MOD_CONTROL,
    MOD_SHIFT,
    MOD_WIN,
    KEY_TO_VK,
    VK_TO_KEY,
    altgr_warning,
    format_hotkey,
    normalize_hotkey,
    validate_hotkey,
)
from ..settings import (
    DEFAULT_DELAY_SECONDS,
    DEFAULT_HOTKEY_CAPTURE,
    DEFAULT_HOTKEY_DELAYED,
    MAX_DELAY_SECONDS,
    MIN_DELAY_SECONDS,
    Settings,
    SettingsStore,
)
from ..output.clipboard import PLACEHOLDERS, build_prompt
from .icons import make_app_icon

COLOR_ERROR = "#E5484D"
COLOR_WARN = "#F59E0B"
COLOR_OK = "#30A46C"

_MODIFIER_KEYS = {
    Qt.Key.Key_Control, Qt.Key.Key_Shift, Qt.Key.Key_Alt, Qt.Key.Key_Meta, Qt.Key.Key_AltGr,
    Qt.Key.Key_CapsLock, Qt.Key.Key_NumLock, Qt.Key.Key_ScrollLock, Qt.Key.Key_Super_L,
    Qt.Key.Key_Super_R, Qt.Key.Key_Hyper_L, Qt.Key.Key_Hyper_R,
}

_MODIFIER_KEY_INTS = frozenset(int(k) for k in _MODIFIER_KEYS)

_QT_NAMED_KEYS = {
    Qt.Key.Key_Space: "Space", Qt.Key.Key_Return: "Enter", Qt.Key.Key_Enter: "Enter",
    Qt.Key.Key_Tab: "Tab", Qt.Key.Key_Escape: "Esc", Qt.Key.Key_Insert: "Insert",
    Qt.Key.Key_Delete: "Delete", Qt.Key.Key_Home: "Home", Qt.Key.Key_End: "End",
    Qt.Key.Key_PageUp: "PageUp", Qt.Key.Key_PageDown: "PageDown", Qt.Key.Key_Left: "Left",
    Qt.Key.Key_Up: "Up", Qt.Key.Key_Right: "Right", Qt.Key.Key_Down: "Down",
    Qt.Key.Key_Print: "PrintScreen", Qt.Key.Key_Pause: "Pause",
}


def qt_modifiers_to_flags(modifiers: Qt.KeyboardModifier) -> int:
    """Qt keyboard modifiers -> hotkeyspec MOD_* bit mask."""
    flags = 0
    if modifiers & Qt.KeyboardModifier.ControlModifier:
        flags |= MOD_CONTROL
    if modifiers & Qt.KeyboardModifier.AltModifier:
        flags |= MOD_ALT
    if modifiers & Qt.KeyboardModifier.ShiftModifier:
        flags |= MOD_SHIFT
    if modifiers & Qt.KeyboardModifier.MetaModifier:
        flags |= MOD_WIN
    return flags


_NAMED_BY_INT = {int(k): v for k, v in _QT_NAMED_KEYS.items()}


def _vk_for_qt_key(key: int, native_vk: int) -> Optional[int]:
    """Windows virtual-key for a key press. Prefer the native VK (layout independent: on a
    Polish layout Ctrl+Alt+S is reported by Qt as a different character but VK 0x53)."""
    if native_vk and native_vk in VK_TO_KEY:
        return native_vk
    key = int(key)
    if int(Qt.Key.Key_A) <= key <= int(Qt.Key.Key_Z) or int(Qt.Key.Key_0) <= key <= int(Qt.Key.Key_9):
        return key  # Qt letter/digit codes equal the ASCII (and VK) codes
    if int(Qt.Key.Key_F1) <= key <= int(Qt.Key.Key_F24):
        return 0x70 + (key - int(Qt.Key.Key_F1))
    named = _NAMED_BY_INT.get(key)
    return KEY_TO_VK.get(named) if named else None


def hotkey_from_key_event(key: int, modifiers: Qt.KeyboardModifier, native_vk: int = 0) -> Optional[str]:
    """Canonical hotkey text ('Ctrl+Alt+S') for a key press, or None when the key is a bare
    modifier or unsupported. Pure function (unit-tested)."""
    if int(key) in _MODIFIER_KEY_INTS:
        return None
    vk = _vk_for_qt_key(int(key), int(native_vk or 0))
    if vk is None:
        return None
    try:
        return format_hotkey(qt_modifiers_to_flags(modifiers), vk)
    except ValueError:
        return None


class HotkeyEdit(QLineEdit):
    """Press-to-record shortcut field: click it, then press the combination."""

    hotkey_changed = Signal(str)
    recording_changed = Signal(bool)

    def __init__(self, text: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(text, parent)
        self.setReadOnly(True)
        self.setToolTip("Click here, then press the shortcut you want. Esc cancels.")
        self.setStyleSheet("QLineEdit { font-weight: 600; padding: 4px 6px; } QLineEdit:focus { border: 2px solid #3E63DD; }")
        self._value_at_focus = text  # what Esc restores

    def value(self) -> str:
        return self.text().strip()

    def set_value(self, text: str) -> None:
        self._value_at_focus = text
        self.setText(text)

    # -- recording -------------------------------------------------------------------
    def event(self, ev: QEvent) -> bool:
        if ev.type() == QEvent.Type.ShortcutOverride:  # let us see Ctrl+... before Qt shortcuts do
            ev.accept()
            return True
        return super().event(ev)

    def keyPressEvent(self, ev: QKeyEvent) -> None:  # noqa: N802 (Qt naming)
        key = ev.key()
        mods = ev.modifiers()
        if int(key) in _MODIFIER_KEY_INTS:
            ev.accept()
            return
        flags = qt_modifiers_to_flags(mods)
        if flags == 0:
            if key == Qt.Key.Key_Escape:  # cancel the recording: back to the value it had on entry
                if self.text() != self._value_at_focus:
                    self.setText(self._value_at_focus)
                    self.hotkey_changed.emit(self._value_at_focus)
                self.clearFocus()
                ev.accept()
                return
            if key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab, Qt.Key.Key_Return, Qt.Key.Key_Enter):
                ev.ignore()  # normal dialog behaviour: move focus / press the default button
                return
            if key in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete):
                ev.accept()
                return
        text = hotkey_from_key_event(key, mods, ev.nativeVirtualKey())
        ev.accept()
        if text is None:
            return
        self.setText(text)
        self.hotkey_changed.emit(text)

    def focusInEvent(self, ev) -> None:  # noqa: N802
        super().focusInEvent(ev)
        self._value_at_focus = self.text()
        self.recording_changed.emit(True)

    def focusOutEvent(self, ev) -> None:  # noqa: N802
        super().focusOutEvent(ev)
        self.recording_changed.emit(False)


class SettingsWindow(QDialog):
    """Small dialog: two hotkey fields (press-to-record; validated with
    hotkeyspec.validate_hotkey, plus the soft hotkeyspec.altgr_warning shown in a note;
    Settings.problems() blocks OK), delay seconds spin box (1-30), output folder (line edit +
    Browse), 'Start with Windows' checkbox, remembered project path / framework hint (editable).
    OK / Apply builds a new Settings and emits `applied`; the controller then saves it,
    re-registers the hotkeys (showing any registration error inline and keeping the dialog open)
    and calls autostart.set_enabled. Cancel changes nothing."""

    applied = Signal(object)  # Settings
    # Extra (optional) signal: True while a hotkey field has the keyboard focus, so the
    # controller can release the global hotkeys and let the user re-record the same combination.
    hotkey_recording = Signal(bool)

    def __init__(self, store: SettingsStore, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._store = store
        self._server_error = ""
        self._info = ""
        self._error_shown = False
        self._recording = False
        s = store.settings

        self.setWindowTitle("UI Report Tool - Settings")
        self.setWindowIcon(make_app_icon())
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setMinimumWidth(560)

        # -- hotkeys
        self.capture_edit = HotkeyEdit(s.hotkey_capture)
        self.delayed_edit = HotkeyEdit(s.hotkey_delayed)
        self.capture_note = self._note_label()
        self.delayed_note = self._note_label()
        self.capture_default = self._default_button(self.capture_edit, DEFAULT_HOTKEY_CAPTURE)
        self.delayed_default = self._default_button(self.delayed_edit, DEFAULT_HOTKEY_DELAYED)

        # -- delay
        self.delay_spin = QSpinBox()
        self.delay_spin.setRange(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS)
        self.delay_spin.setSuffix(" seconds")
        self.delay_spin.setValue(int(s.delay_seconds) if MIN_DELAY_SECONDS <= int(s.delay_seconds) <= MAX_DELAY_SECONDS else DEFAULT_DELAY_SECONDS)
        self.delay_spin.setToolTip("Countdown shown before the screen freezes in a delayed capture.")

        # -- folders
        self.output_edit = QLineEdit(s.output_root)
        self.output_browse = QPushButton("Browse...")
        self.output_browse.clicked.connect(lambda: self._browse(self.output_edit, "Choose the reports folder"))
        self.project_edit = QLineEdit(s.last_project_path)
        self.project_edit.setPlaceholderText("e.g. C:\\dev\\my-app (filled in from your last session)")
        self.project_browse = QPushButton("Browse...")
        self.project_browse.clicked.connect(lambda: self._browse(self.project_edit, "Choose your project folder"))
        self.framework_edit = QLineEdit(s.last_framework_hint)
        self.framework_edit.setPlaceholderText("e.g. React + Tailwind, WPF, Qt Widgets")

        self.autostart_check = QCheckBox("Start UI Report Tool when I sign in to Windows")
        self.autostart_check.setChecked(bool(s.start_with_windows))

        # -- text copied on Finish (empty = the built-in prompt for Claude)
        self.copy_text_edit = QPlainTextEdit(s.copy_text_template)
        self.copy_text_edit.setTabChangesFocus(True)
        example = build_prompt(3, "", Path(s.output_root or os.path.expanduser("~")) / "<date>_<goal>" / "report.md")
        self.copy_text_edit.setPlaceholderText(f"Empty = the built-in text for Claude:\n{example}")
        self.copy_text_edit.setFixedHeight(self.copy_text_edit.fontMetrics().lineSpacing() * 4 + 14)
        self.copy_text_help = QLabel("Fill-ins: " + "  ".join("{" + name + "}" for name in PLACEHOLDERS))
        self.copy_text_help.setToolTip("\n".join("{" + name + "}  " + what for name, what in PLACEHOLDERS.items()))
        self.copy_text_help.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.copy_text_default = QToolButton()
        self.copy_text_default.setText("Default")
        self.copy_text_default.setToolTip("Go back to the built-in text for Claude")
        self.copy_text_default.clicked.connect(self.copy_text_edit.clear)
        self.paste_check = QCheckBox("On Finish, also paste it into the Claude app if it's open (you press Enter)")
        self.paste_check.setChecked(bool(s.paste_into_claude))
        self.paste_check.setToolTip("Brings the Claude desktop app to the front and pastes the text into it. "
                                    "Claude is never started, and nothing is sent until you press Enter.")

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.addRow("Capture hotkey", self._with_button(self.capture_edit, self.capture_default))
        form.addRow("", self.capture_note)
        form.addRow("Delayed capture hotkey", self._with_button(self.delayed_edit, self.delayed_default))
        form.addRow("", self.delayed_note)
        form.addRow("Delay length", self.delay_spin)
        form.addRow("Reports folder", self._with_button(self.output_edit, self.output_browse))
        form.addRow("Project folder", self._with_button(self.project_edit, self.project_browse))
        form.addRow("App / framework", self.framework_edit)
        form.addRow("", self.autostart_check)
        form.addRow("Text copied on Finish", self._with_button(self.copy_text_edit, self.copy_text_default))
        form.addRow("", self.copy_text_help)
        form.addRow("", self.paste_check)

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Apply
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.ok_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.apply_button = self.buttons.button(QDialogButtonBox.StandardButton.Apply)
        self.cancel_button = self.buttons.button(QDialogButtonBox.StandardButton.Cancel)
        self.ok_button.clicked.connect(lambda: self._emit_apply(close=True))
        self.apply_button.clicked.connect(lambda: self._emit_apply(close=False))
        self.cancel_button.clicked.connect(self.reject)
        self.ok_button.setDefault(True)

        root = QVBoxLayout(self)
        root.addLayout(form)
        root.addWidget(self.status_label)
        root.addWidget(self.buttons)

        for edit in (self.capture_edit, self.delayed_edit):
            edit.hotkey_changed.connect(self._on_changed)
            edit.recording_changed.connect(self._on_recording_changed)
        self.delay_spin.valueChanged.connect(self._on_changed)
        for edit in (self.output_edit, self.project_edit, self.framework_edit):
            edit.textChanged.connect(self._on_changed)
        self.autostart_check.toggled.connect(self._on_changed)
        self.copy_text_edit.textChanged.connect(self._on_changed)
        self.paste_check.toggled.connect(self._on_changed)

        self._revalidate()

    # ------------------------------------------------------------------ small builders
    @staticmethod
    def _note_label() -> QLabel:
        lab = QLabel()
        lab.setWordWrap(True)
        lab.setVisible(False)
        return lab

    @staticmethod
    def _with_button(field: QWidget, button: QWidget) -> QWidget:
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(field, 1)
        lay.addWidget(button, 0)
        return box

    def _default_button(self, edit: HotkeyEdit, default: str) -> QToolButton:
        btn = QToolButton()
        btn.setText("Default")
        btn.setToolTip(f"Reset to {default}")
        btn.clicked.connect(lambda: (edit.set_value(default), self._on_changed()))
        return btn

    def _browse(self, edit: QLineEdit, title: str) -> None:  # pragma: no cover - modal dialog
        start = edit.text().strip() or os.path.expanduser("~")
        chosen = QFileDialog.getExistingDirectory(self, title, start)
        if chosen:
            edit.setText(os.path.normpath(chosen))

    # ------------------------------------------------------------------ state
    def current_settings(self) -> Settings:
        """The Settings the widgets currently describe (not yet saved)."""

        def hk(edit: HotkeyEdit) -> str:
            text = edit.value()
            return normalize_hotkey(text) if validate_hotkey(text) is None else text

        return replace(
            self._store.settings,
            hotkey_capture=hk(self.capture_edit),
            hotkey_delayed=hk(self.delayed_edit),
            delay_seconds=int(self.delay_spin.value()),
            output_root=self.output_edit.text().strip(),
            start_with_windows=self.autostart_check.isChecked(),
            last_project_path=self.project_edit.text().strip(),
            last_framework_hint=self.framework_edit.text().strip(),
            copy_text_template=self.copy_text_edit.toPlainText().strip(),
            paste_into_claude=self.paste_check.isChecked(),
        )

    def problems(self) -> list[str]:
        return self.current_settings().problems()

    def show_error(self, message: str) -> None:
        """Display an inline error (e.g. 'Ctrl+Alt+S is already used by another program')."""
        self._server_error = str(message)
        self._info = ""
        self._error_shown = True
        self._update_status()

    def _on_changed(self, *_args) -> None:
        self._server_error = ""
        self._info = ""
        self._revalidate()

    def _hotkey_note(self, edit: HotkeyEdit, note: QLabel) -> None:
        text = edit.value()
        err = validate_hotkey(text)
        if err:
            note.setText(err)
            note.setStyleSheet(f"color: {COLOR_ERROR}; font-size: 11px;")
            note.setVisible(True)
            return
        warn = altgr_warning(text)
        if warn:
            note.setText(warn)
            note.setStyleSheet(f"color: {COLOR_WARN}; font-size: 11px;")
            note.setVisible(True)
            return
        note.clear()
        note.setVisible(False)

    def _revalidate(self) -> None:
        self._hotkey_note(self.capture_edit, self.capture_note)
        self._hotkey_note(self.delayed_edit, self.delayed_note)
        ok = not self.problems()
        self.ok_button.setEnabled(ok)
        self.apply_button.setEnabled(ok)
        self._update_status()

    def _update_status(self) -> None:
        if self._server_error:
            self.status_label.setStyleSheet(f"color: {COLOR_ERROR};")
            self.status_label.setText(self._server_error)
            self.status_label.setVisible(True)
            return
        # hotkey-specific problems are already shown next to their field
        rest = [p for p in self.problems() if " hotkey: " not in p]
        if rest:
            self.status_label.setStyleSheet(f"color: {COLOR_ERROR};")
            self.status_label.setText("\n".join(rest))
            self.status_label.setVisible(True)
        elif self._info:
            self.status_label.setStyleSheet(f"color: {COLOR_OK};")
            self.status_label.setText(self._info)
            self.status_label.setVisible(True)
        else:
            self.status_label.clear()
            self.status_label.setVisible(False)

    def _emit_apply(self, close: bool) -> None:
        if self.problems():
            self._revalidate()
            return
        self._server_error = ""
        self._error_shown = False
        self.applied.emit(self.current_settings())  # direct connection: the controller runs now
        if self._error_shown:
            return  # the controller reported an error via show_error(): keep the dialog open
        if close:
            self.accept()
        else:
            self._info = "Settings applied."
            self._update_status()

    # ------------------------------------------------------------------ hotkey recording
    def _on_recording_changed(self, _focused: bool) -> None:
        QTimer.singleShot(0, self._sync_recording)

    def _sync_recording(self) -> None:
        active = self.capture_edit.hasFocus() or self.delayed_edit.hasFocus()
        if active != self._recording:
            self._recording = active
            self.hotkey_recording.emit(active)

    def done(self, result: int) -> None:  # noqa: D401 - Qt override
        super().done(result)
        if self._recording:
            self._recording = False
            self.hotkey_recording.emit(False)
