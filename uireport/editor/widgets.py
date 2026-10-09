"""Small reusable widgets for the editor. Owner: editor builder (package B)."""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtWidgets import QButtonGroup, QHBoxLayout, QPushButton, QWidget

from ..models import DEFAULT_ROLE, ROLE_ORDER, Role


def _segment_qss(color: str, position: str) -> str:
    """Style of one segment. Colours other than the role colour come from the palette so the
    control follows the system light / dark theme."""
    r = "6px"
    left = r if position in ("first", "only") else "0px"
    right = r if position in ("last", "only") else "0px"
    left_border = "1px" if position in ("first", "only") else "0px"
    return f"""
    QPushButton {{
        padding: 4px 14px;
        border: 1px solid palette(mid);
        border-left-width: {left_border};
        border-bottom: 3px solid {color};
        background: palette(button);
        color: palette(button-text);
        border-top-left-radius: {left};
        border-bottom-left-radius: {left};
        border-top-right-radius: {right};
        border-bottom-right-radius: {right};
    }}
    QPushButton:hover {{ background: palette(midlight); }}
    QPushButton:checked {{
        background: {color};
        border-color: {color};
        color: #FFFFFF;
        font-weight: 600;
    }}
    QPushButton:disabled {{ color: palette(mid); border-bottom-color: palette(mid); }}
    """


class RoleSegmented(QWidget):
    """One-click segmented control of exclusive buttons: Shot / Problem / Want / Context / After.
    Tooltips are `Role.description`; the checked segment is filled with `Role.color`."""

    role_changed = Signal(object)  # Role, only for user clicks and set_role(emit=True)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self.buttons: dict[Role, QPushButton] = {}
        n = len(ROLE_ORDER)
        for i, role in enumerate(ROLE_ORDER):
            btn = QPushButton(role.label, self)
            btn.setCheckable(True)
            btn.setToolTip(role.description)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # clicking never pulls focus out of the caption
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            pos = "only" if n == 1 else "first" if i == 0 else "last" if i == n - 1 else "mid"
            btn.setStyleSheet(_segment_qss(role.color, pos))
            btn.setProperty("role", role.value)
            btn.clicked.connect(lambda _checked=False, r=role: self._on_clicked(r))
            self._group.addButton(btn)
            lay.addWidget(btn)
            self.buttons[role] = btn
        self.buttons[DEFAULT_ROLE].setChecked(True)

    def _on_clicked(self, role: Role) -> None:
        self.buttons[role].setChecked(True)
        self.role_changed.emit(role)

    def role(self) -> Role:
        for r, b in self.buttons.items():
            if b.isChecked():
                return r
        return DEFAULT_ROLE

    def set_role(self, role: Role, emit: bool = False) -> None:
        role = Role(role)
        self.buttons[role].setChecked(True)
        if emit:
            self.role_changed.emit(role)


class EnterShortcutFilter(QObject):
    """Event filter for multi-line text widgets: makes sure Ctrl+Enter, Ctrl+Alt+Enter and
    Ctrl+Shift+Enter (main Return and keypad Enter alike) run the editor's Next / Next
    (delayed) / Finish actions even if the widget would otherwise treat the key press as text.
    The window-wide QShortcuts normally fire first; this is the belt-and-braces fallback and
    guarantees the key is never inserted as a line break."""

    def __init__(
        self,
        on_next: Callable[[], None],
        on_delayed: Callable[[], None],
        on_finish: Callable[[], None],
        parent: Optional[QObject] = None,
        on_finish_to: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__(parent)
        self._next, self._delayed, self._finish = on_next, on_delayed, on_finish
        self._finish_to = on_finish_to

    def eventFilter(self, obj: QObject, ev: QEvent) -> bool:  # noqa: N802
        if ev.type() == QEvent.Type.KeyPress and ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):  # type: ignore[attr-defined]
            mods = ev.modifiers() & ~Qt.KeyboardModifier.KeypadModifier  # type: ignore[attr-defined]
            ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
            alt = bool(mods & Qt.KeyboardModifier.AltModifier)
            shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
            handler = None
            if ctrl and alt and shift:
                handler = self._finish_to
            elif ctrl and alt and not shift:
                handler = self._delayed
            elif ctrl and shift and not alt:
                handler = self._finish
            elif ctrl and not alt and not shift:
                handler = self._next
            if handler is not None:
                handler()
                return True
        return False
