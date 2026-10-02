"""Session-level optional fields. Owner: editor builder (package B)."""
from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import QDir, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..models import Session

FRAMEWORK_SUGGESTIONS = [
    "React + Tailwind",
    "React",
    "Next.js",
    "Vue",
    "Svelte",
    "Angular",
    "HTML + CSS",
    "WPF",
    "WinUI 3",
    "WinForms",
    ".NET MAUI",
    "Qt/QML",
    "Qt Widgets",
    "Flutter",
    "Electron",
    "Tauri",
    "SwiftUI",
    "Jetpack Compose",
]


class SessionFieldsWidget(QWidget):
    """Edits session.goal (1-2 sentences), session.project_path (line edit + Browse...
    folder dialog), session.framework_hint (editable combo with suggestions such as
    'React + Tailwind', 'WPF', 'WinUI 3', 'Qt/QML', 'Flutter', 'Electron', 'SwiftUI'),
    session.expected and session.actual. Writes straight into the Session object on every
    edit and emits `changed`. All fields optional. Collapsible so the editor stays compact.

    The section starts collapsed; while collapsed a one-line summary of what is filled in is
    shown next to the header. Extra API: `set_expanded`, `is_expanded`, signal `expanded_changed`.
    """

    changed = Signal()
    expanded_changed = Signal(bool)

    def __init__(self, session: Session, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._session = session
        self._loading = False

        # header ---------------------------------------------------------
        self._toggle = QToolButton(self)
        self._toggle.setText("Session details (optional)")
        self._toggle.setCheckable(True)
        self._toggle.setChecked(False)
        self._toggle.setAutoRaise(True)
        self._toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._toggle.setArrowType(Qt.ArrowType.RightArrow)
        self._toggle.setToolTip("Goal, project folder, framework, expected vs. actual: all optional")
        self._toggle.setStyleSheet(
            "QToolButton { border: none; background: transparent; padding: 3px 4px; }"
            "QToolButton:hover { background: palette(midlight); border-radius: 4px; }"
        )
        self._toggle.toggled.connect(self._on_toggled)
        self._summary = QLabel(self)
        self._summary.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        pal = self._summary.palette()
        pal.setColor(self._summary.foregroundRole(), pal.placeholderText().color())
        self._summary.setPalette(pal)
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.addWidget(self._toggle)
        head.addWidget(self._summary, 1)

        # body -----------------------------------------------------------
        self.goal_edit = self._make_text("What do you want done? One or two sentences.")
        self.expected_edit = self._make_text("Expected behaviour / look")
        self.actual_edit = self._make_text("Actual behaviour / look")
        self.project_edit = QLineEdit(self)
        self.project_edit.setPlaceholderText(r"Project folder, e.g. C:\dev\my-app")
        self.project_edit.setClearButtonEnabled(True)
        self.browse_button = QPushButton("Browse...", self)
        self.browse_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.framework_combo = QComboBox(self)
        self.framework_combo.setEditable(True)
        self.framework_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.framework_combo.addItems(FRAMEWORK_SUGGESTIONS)
        self.framework_combo.setCurrentIndex(-1)
        self.framework_combo.lineEdit().setPlaceholderText("App / framework, e.g. WPF")
        self.framework_combo.lineEdit().setClearButtonEnabled(True)

        self._body = QWidget(self)
        grid = QGridLayout(self._body)
        grid.setContentsMargins(0, 2, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(2)
        for col, (label, widget) in enumerate(
            (("Goal", self.goal_edit), ("Expected", self.expected_edit), ("Actual", self.actual_edit))
        ):
            grid.addWidget(QLabel(label, self._body), 0, col)
            grid.addWidget(widget, 1, col)
        proj_row = QHBoxLayout()
        proj_row.setContentsMargins(0, 0, 0, 0)
        proj_row.addWidget(self.project_edit, 1)
        proj_row.addWidget(self.browse_button)
        grid.addWidget(QLabel("Project folder", self._body), 2, 0, 1, 2)
        grid.addWidget(QLabel("App / framework", self._body), 2, 2)
        grid.addLayout(proj_row, 3, 0, 1, 2)
        grid.addWidget(self.framework_combo, 3, 2)
        for c in range(3):
            grid.setColumnStretch(c, 1)
        self._body.setVisible(False)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addLayout(head)
        root.addWidget(self._body)

        # wiring ---------------------------------------------------------
        self.goal_edit.textChanged.connect(lambda: self._write("goal", self.goal_edit.toPlainText()))
        self.expected_edit.textChanged.connect(lambda: self._write("expected", self.expected_edit.toPlainText()))
        self.actual_edit.textChanged.connect(lambda: self._write("actual", self.actual_edit.toPlainText()))
        self.project_edit.textChanged.connect(lambda t: self._write("project_path", t))
        self.framework_combo.editTextChanged.connect(lambda t: self._write("framework_hint", t))
        self.browse_button.clicked.connect(self._browse)
        self.refresh()

    # ------------------------------------------------------------------
    def _make_text(self, placeholder: str) -> QPlainTextEdit:
        w = QPlainTextEdit(self)
        w.setPlaceholderText(placeholder)
        w.setTabChangesFocus(True)
        w.setFixedHeight(w.fontMetrics().lineSpacing() * 3 + 14)
        return w

    def set_session(self, session: Session) -> None:
        """Bind to a (new) session and reload the widgets from it."""
        self._session = session
        self.refresh()

    def refresh(self) -> None:
        s = self._session
        self._loading = True
        try:
            for edit, value in (
                (self.goal_edit, s.goal),
                (self.expected_edit, s.expected),
                (self.actual_edit, s.actual),
            ):
                if edit.toPlainText().strip() != value:  # never rewrite what the user is typing
                    edit.setPlainText(value)
            if self.project_edit.text().strip() != s.project_path:
                self.project_edit.setText(s.project_path)
            if self.framework_combo.currentText().strip() != s.framework_hint:
                self.framework_combo.setCurrentIndex(-1)
                self.framework_combo.setEditText(s.framework_hint)
        finally:
            self._loading = False
        self._update_summary()

    # ------------------------------------------------------------------
    def is_expanded(self) -> bool:
        return self._toggle.isChecked()

    def set_expanded(self, expanded: bool) -> None:
        self._toggle.setChecked(bool(expanded))

    def _on_toggled(self, checked: bool) -> None:
        self._body.setVisible(checked)
        self._toggle.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)
        self._update_summary()
        self.expanded_changed.emit(checked)

    def _update_summary(self) -> None:
        if self._toggle.isChecked():
            self._summary.setText("")
            return
        s = self._session
        parts: list[str] = []
        goal = " ".join(s.goal.split())
        if goal:
            parts.append(goal if len(goal) <= 70 else goal[:69] + "\u2026")
        if s.framework_hint.strip():
            parts.append(s.framework_hint.strip())
        if s.project_path.strip():
            parts.append(os.path.basename(s.project_path.rstrip("\\/")) or s.project_path)
        self._summary.setText("  \u00b7  ".join(parts))

    def _write(self, attr: str, value: str) -> None:
        if self._loading:
            return
        setattr(self._session, attr, value.strip())
        self._update_summary()
        self.changed.emit()

    def _browse(self) -> None:
        start = self._session.project_path.strip()
        if not start or not os.path.isdir(start):
            start = os.path.expanduser("~")
        chosen = QFileDialog.getExistingDirectory(self, "Choose the project folder", start)
        if chosen:
            self.project_edit.setText(QDir.toNativeSeparators(chosen))
