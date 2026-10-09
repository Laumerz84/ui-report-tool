"""The compact editor window. Owner: editor builder (package B)."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QEventLoop, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QKeySequence, QShortcut, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from .. import APP_NAME
from ..models import Role, Session, ROLE_ORDER, MonitorMeta
from . import placement
from .canvas import TOOL_KEYS, TOOL_LABELS, TOOL_TIPS, AnnotationCanvas, Tool
from .marks_list import MarksList
from .session_fields import SessionFieldsWidget
from .session_panel import SessionPanel
from .widgets import EnterShortcutFilter, RoleSegmented

_RETURN_KEYS = ("Return", "Enter")  # main Return and keypad Enter


class EditorWindow(QMainWindow):
    """One window containing: AnnotationCanvas, toolbar (tools), caption box, role toggle,
    pin list with notes, SessionPanel, SessionFieldsWidget and the buttons.

    It edits the shared `Session` / `Shot` objects IN PLACE (caption, role, annotations,
    session fields, reorder, delete) and only emits signals; it never captures, writes
    files, or touches settings.

    Buttons / shortcuts (window-wide, must work while the caption box has focus; both
    Return and the keypad Enter):
        Next            Ctrl+Enter        -> next_requested
        Next (delayed)  Ctrl+Alt+Enter    -> next_delayed_requested
        Finish          Ctrl+Shift+Enter  -> finish_requested (disabled while session has no shots)
        Finish to...    Ctrl+Alt+Shift+Enter -> finish_to_requested (pick which Claude pane to paste into)
    Role toggle: four one-click buttons Problem / Want / Context / After (default Problem,
    tooltips = Role.description), Alt+1..4. Tool shortcuts only when the canvas has focus.
    Closing the window (X) just hides it: the session and its shots are kept; it is not
    Finish and not a discard. An explicit 'Discard session' button asks for confirmation
    and then emits discard_session_requested.

    Signals:
        next_requested()
        next_delayed_requested()
        finish_requested()
        finish_to_requested()
        discard_session_requested()
        session_changed()   any mutation: caption/role/annotation/session field/reorder/delete

    Widgets worth knowing about (used by the tests): `canvas`, `panel`, `fields`, `marks`,
    `caption_edit`, `role_control` (`role_buttons`), `tool_buttons`, `next_button`,
    `next_delayed_button`, `finish_button`, `discard_button`.
    """

    next_requested = Signal()
    next_delayed_requested = Signal()
    finish_requested = Signal()
    finish_to_requested = Signal()
    discard_session_requested = Signal()
    session_changed = Signal()

    def __init__(self, session: Session, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._session = session
        self._current_id: Optional[str] = None
        self._loading = False
        self._placed = False
        self.setWindowTitle(APP_NAME)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)  # hiding this window never quits the tray app
        self._build_ui()
        self._install_shortcuts()
        self._wire()
        self._show_empty()
        self.refresh()

    # ==================================================================
    # construction
    # ==================================================================
    def _build_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 6, 8, 8)
        root.setSpacing(6)

        # ---- tool row -------------------------------------------------
        self.canvas = AnnotationCanvas(self)
        self.marks = MarksList(self)
        self.tool_bar = QWidget(self)
        bar = QHBoxLayout(self.tool_bar)
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(4)
        self._tool_group = QButtonGroup(self)
        self._tool_group.setExclusive(True)
        self.tool_buttons: dict[Tool, QToolButton] = {}
        for tool in Tool:
            btn = QToolButton(self.tool_bar)
            btn.setText(TOOL_LABELS[tool])
            btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
            btn.setCheckable(True)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.setToolTip(f"{TOOL_TIPS[tool]}  [key {TOOL_KEYS[tool]} while the image has focus]")
            btn.clicked.connect(lambda _c=False, t=tool: self.canvas.set_tool(t))
            self._tool_group.addButton(btn)
            bar.addWidget(btn)
            self.tool_buttons[tool] = btn
        self.tool_buttons[self.canvas.tool()].setChecked(True)
        bar.addSpacing(8)
        self.undo_button = self._small_button("Undo", "Undo the last change to this screenshot (Ctrl+Z, image focused)", bar)
        self.redo_button = self._small_button("Redo", "Redo (Ctrl+Y, image focused)", bar)
        self.delete_mark_button = self._small_button("Delete", "Delete the selected annotation (Delete, image focused)", bar)
        bar.addSpacing(8)
        self.fit_button = self._small_button("Fit", "Fit the whole screenshot into the window (F)", bar, checkable=True)
        self.actual_button = self._small_button("1:1", "One image pixel per screen pixel (1)", bar, checkable=True)
        self.zoom_out_button = self._small_button("−", "Zoom out (-, or Ctrl+wheel)", bar)
        self.zoom_in_button = self._small_button("+", "Zoom in (+, or Ctrl+wheel)", bar)
        self.zoom_label = QLabel("", self.tool_bar)
        self.zoom_label.setMinimumWidth(self.fontMetrics().horizontalAdvance("000%") + 6)
        bar.addWidget(self.zoom_label)
        bar.addStretch(1)
        root.addWidget(self.tool_bar)

        # ---- canvas + marks -------------------------------------------
        marks_box = QWidget(self)
        mb = QVBoxLayout(marks_box)
        mb.setContentsMargins(0, 0, 0, 0)
        mb.setSpacing(2)
        marks_title = QLabel("Marks", marks_box)
        marks_title.setToolTip("Pins, measurements and boxes of this screenshot. Double-click a pin's note to edit it.")
        mb.addWidget(marks_title)
        mb.addWidget(self.marks, 1)
        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self.canvas)
        self.splitter.addWidget(marks_box)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setSizes([760, 260])
        root.addWidget(self.splitter, 1)

        # ---- role + caption -------------------------------------------
        role_row = QHBoxLayout()
        role_row.setContentsMargins(0, 0, 0, 0)
        role_row.setSpacing(8)
        role_row.addWidget(QLabel("Role", self))
        self.role_control = RoleSegmented(self)
        self.role_buttons = self.role_control.buttons
        role_row.addWidget(self.role_control)
        hint = QLabel(f"Alt+1 … Alt+{len(ROLE_ORDER)}", self)
        hint.setEnabled(False)
        role_row.addWidget(hint)
        role_row.addStretch(1)
        root.addLayout(role_row)

        self.caption_edit = QPlainTextEdit(self)
        self.caption_edit.setPlaceholderText(
            "Caption: what is wrong (or wanted) here?  Enter = new line, Ctrl+Enter = next screenshot"
        )
        self.caption_edit.setTabChangesFocus(True)
        lh = self.caption_edit.fontMetrics().lineSpacing()
        self.caption_edit.setMinimumHeight(lh * 2 + 16)
        self.caption_edit.setMaximumHeight(lh * 4 + 16)
        root.addWidget(self.caption_edit)

        # ---- session fields + filmstrip -------------------------------
        self.fields = SessionFieldsWidget(self._session, self)
        root.addWidget(self.fields)
        self.panel = SessionPanel(self._session, self)
        root.addWidget(self.panel)

        # ---- buttons --------------------------------------------------
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.discard_button = QPushButton("Discard session", self)
        self.discard_button.setToolTip("Throw away every screenshot of this session (asks first)")
        self.next_button = QPushButton("Next  (Ctrl+Enter)", self)
        self.next_button.setToolTip("Keep this shot and capture the next one right away")
        self.next_delayed_button = QPushButton("Next (delayed)  (Ctrl+Alt+Enter)", self)
        self.next_delayed_button.setToolTip(
            "Keep this shot, show a countdown, then capture the next one (open menus / hover states first)"
        )
        self.finish_button = QPushButton("Finish  (Ctrl+Shift+Enter)", self)
        self.finish_button.setToolTip(
            "Save every shot and build the report (Ctrl+Alt+Shift+Enter: choose which Claude pane to paste into)")
        self.finish_button.setStyleSheet("QPushButton { font-weight: 600; }")  # inherits the size, only bolds
        self.shot_label = QLabel("", self)
        row.addWidget(self.discard_button)
        row.addSpacing(8)
        row.addWidget(self.shot_label)
        row.addStretch(1)
        row.addWidget(self.next_button)
        row.addWidget(self.next_delayed_button)
        row.addWidget(self.finish_button)
        root.addLayout(row)

        self.setTabOrder(self.caption_edit, self.fields)

    def _small_button(self, text: str, tip: str, layout: QHBoxLayout, checkable: bool = False) -> QToolButton:
        b = QToolButton(self.tool_bar)
        b.setText(text)
        b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        b.setToolTip(tip)
        b.setCheckable(checkable)
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b.setMinimumWidth(b.fontMetrics().horizontalAdvance(text) + 18)
        layout.addWidget(b)
        return b

    def _install_shortcuts(self) -> None:
        """Window-wide shortcuts. They fire while any child (the caption box included) has
        focus; Return and the keypad Enter are both bound."""
        def bind(seq: str, slot) -> None:
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.ShortcutContext.WindowShortcut)
            sc.setAutoRepeat(False)
            sc.activated.connect(slot)

        for key in _RETURN_KEYS:
            bind(f"Ctrl+{key}", self._do_next)
            bind(f"Ctrl+Alt+{key}", self._do_next_delayed)
            bind(f"Ctrl+Shift+{key}", self._do_finish)
            bind(f"Ctrl+Alt+Shift+{key}", self._do_finish_to)
        for i, role in enumerate(ROLE_ORDER, start=1):
            bind(f"Alt+{i}", lambda r=role: self._set_role_from_key(r))
        # multi-line boxes: make sure the same keys are never taken as text
        self._enter_filter = EnterShortcutFilter(self._do_next, self._do_next_delayed, self._do_finish, self,
                                                 on_finish_to=self._do_finish_to)
        for w in (self.caption_edit, self.fields.goal_edit, self.fields.expected_edit, self.fields.actual_edit):
            w.installEventFilter(self._enter_filter)

    def _wire(self) -> None:
        c = self.canvas
        c.annotations_changed.connect(self._on_annotations_changed)
        c.selection_changed.connect(self._on_canvas_selection)
        c.history_changed.connect(self._sync_history_buttons)
        c.tool_changed.connect(self._on_tool_changed)
        c.view_changed.connect(self._sync_view_controls)
        self.marks.annotation_selected.connect(c.select)
        self.marks.note_edited.connect(c.set_pin_note)
        self.marks.delete_requested.connect(c.delete_annotation)

        self.undo_button.clicked.connect(c.undo)
        self.redo_button.clicked.connect(c.redo)
        self.delete_mark_button.clicked.connect(c.delete_selected)
        self.fit_button.clicked.connect(lambda: c.zoom_fit())
        self.actual_button.clicked.connect(lambda: c.zoom_actual())
        self.zoom_in_button.clicked.connect(c.zoom_in)
        self.zoom_out_button.clicked.connect(c.zoom_out)

        self.caption_edit.textChanged.connect(self._on_caption_changed)
        self.role_control.role_changed.connect(self._on_role_changed)

        self.panel.shot_selected.connect(self.show_shot)
        self.panel.shot_delete_requested.connect(self._delete_shot)
        self.panel.order_changed.connect(self._reorder)
        self.fields.changed.connect(self.session_changed)

        self.next_button.clicked.connect(self._do_next)
        self.next_delayed_button.clicked.connect(self._do_next_delayed)
        self.finish_button.clicked.connect(self._do_finish)
        self.discard_button.clicked.connect(self._on_discard)

    # ==================================================================
    # public API
    # ==================================================================
    def set_session(self, session: Session) -> None:
        """Bind to a new Session (after Finish/Discard); clears the canvas."""
        self._flush_pending()
        self._session = session
        self.fields.set_session(session)
        self.panel.set_session(session)
        self._show_empty()
        self._update_state()

    def refresh(self) -> None:
        """Re-read session.shots into the SessionPanel / buttons (call after the controller
        added a shot). Does not change which shot is being edited."""
        self.panel.refresh()
        self.fields.refresh()
        cur = self._current_id
        if cur is not None and self._session.get_shot(cur) is None:
            # the open shot vanished (removed behind our back): open its neighbour or show the empty state
            shots = self._session.shots
            if shots:
                self.show_shot(shots[min(self._last_index, len(shots) - 1)].id)
            else:
                self._show_empty()
        else:
            self.panel.set_current(cur)
        self._update_state()

    def show_shot(self, shot_id: str) -> None:
        """Load that shot into the canvas / caption / role controls and FOCUS THE CAPTION
        BOX (cursor at the end). Unknown id -> ignored."""
        shot = self._session.get_shot(shot_id)
        if shot is None:
            return
        self._flush_pending()
        if not (shot_id == self._current_id and self.canvas.shot() is shot):
            self._current_id = shot_id
            self.canvas.set_shot(shot)
            self.marks.set_shot(shot)
        self._last_index = max(0, self._session.index_of(shot_id))
        self._set_enabled(True)
        self._loading = True
        try:
            if self.caption_edit.toPlainText() != shot.caption:
                self.caption_edit.setPlainText(shot.caption)
            self.role_control.set_role(shot.role)
        finally:
            self._loading = False
        self.panel.set_current(shot_id)
        self._update_state()
        self._focus_caption()

    def current_shot_id(self) -> Optional[str]:
        return self._current_id

    def present(self, monitor_hint: Optional[MonitorMeta] = None) -> None:
        """Show, restore from minimised, raise and activate the window (use a foreground
        trick if Windows refuses activation), placed on the monitor `monitor_hint` (the
        one just captured) when given. Afterwards QApplication.focusWidget() is the caption
        box and the window sits inside the monitor's available area."""
        screen = placement.find_screen_for_monitor(monitor_hint) if monitor_hint is not None else None
        if screen is None:
            screen = self.screen() if self._placed else QGuiApplication.primaryScreen()
        ag = screen.availableGeometry()
        avail = (ag.x(), ag.y(), ag.width(), ag.height())
        min_sz = self.minimumSizeHint()
        moved_screen = monitor_hint is not None and self._placed and self.screen() is not screen
        if not self._placed or moved_screen:
            size = (self.width(), self.height()) if self._placed else placement.compact_size(avail)
            x, y, w, h = placement.place_centered(avail, size, (min_sz.width(), min_sz.height()))
            self.winId()  # make sure a QWindow exists so it can be pinned to the target screen
            handle = self.windowHandle()
            if handle is not None and handle.screen() is not screen:
                handle.setScreen(screen)
            self.setGeometry(x, y, w, h)
            self._placed = True
        if self.isMinimized():
            self.setWindowState((self.windowState() & ~Qt.WindowState.WindowMinimized) | Qt.WindowState.WindowActive)
        self.show()
        self._keep_frame_inside(avail)
        self.raise_()
        self.activateWindow()
        if QGuiApplication.platformName() == "windows":
            try:
                placement.bring_to_front(int(self.winId()))
            except Exception:
                pass
        self._focus_caption()
        QTimer.singleShot(0, self._focus_caption)

    def hide_for_capture(self) -> None:
        """Hide right now so the next grab does not contain the editor: hide(), flush
        pending paint events, winapi-independent. Also called with the window already
        hidden (no-op). The controller adds the compositor settle wait itself."""
        self._flush_pending()
        QToolTip.hideText()
        self.hide()
        # flush paint / window-system events; user input is left queued so a second, repeated
        # key press cannot re-enter the controller from inside this call
        QApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)

    # ==================================================================
    # window behaviour
    # ==================================================================
    def closeEvent(self, event) -> None:  # noqa: N802
        """X only hides: the session and its shots are kept."""
        self._flush()
        event.ignore()
        self.hide()

    def _keep_frame_inside(self, avail: placement.Rect4) -> None:
        fg = self.frameGeometry()
        rect = (fg.x(), fg.y(), fg.width(), fg.height())
        if placement.rect_inside(rect, avail):
            return
        nx, ny, nw, nh = placement.clamp_rect_into(rect, avail)
        self.move(self.x() + (nx - fg.x()), self.y() + (ny - fg.y()))
        if nw < fg.width() or nh < fg.height():
            self.resize(self.width() - (fg.width() - nw), self.height() - (fg.height() - nh))

    def _focus_caption(self) -> None:
        if not self.caption_edit.isEnabled():
            return
        self.caption_edit.setFocus(Qt.FocusReason.OtherFocusReason)
        cur = self.caption_edit.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End)
        self.caption_edit.setTextCursor(cur)

    # ==================================================================
    # state
    # ==================================================================
    _last_index = 0

    def _set_enabled(self, on: bool) -> None:
        for w in (self.caption_edit, self.role_control, self.tool_bar):
            w.setEnabled(on)

    def _show_empty(self) -> None:
        """No shot open: canvas cleared, caption / role / tools disabled."""
        self._current_id = None
        self.canvas.set_shot(None)
        self.marks.set_shot(None)
        self._loading = True
        try:
            self.caption_edit.clear()
        finally:
            self._loading = False
        self._set_enabled(False)
        self.panel.set_current(None)
        self._update_state()

    def _update_state(self) -> None:
        shots = self._session.shots
        n = len(shots)
        self.finish_button.setEnabled(n > 0)
        self.finish_button.setToolTip(
            "Save every shot and build the report (Ctrl+Alt+Shift+Enter: choose which Claude pane to paste into)"
            if n else "Take at least one screenshot first"
        )
        if self._current_id is not None and self._session.get_shot(self._current_id) is not None:
            i = self._session.index_of(self._current_id) + 1
            self.shot_label.setText(f"Shot {i} of {n}")
            self.setWindowTitle(f"{APP_NAME} - shot {i} of {n}")
        else:
            self.shot_label.setText("No screenshot open" if n else "No screenshots yet")
            self.setWindowTitle(APP_NAME)
        self._sync_history_buttons()
        self._sync_view_controls()

    def _sync_history_buttons(self) -> None:
        self.undo_button.setEnabled(self.canvas.can_undo())
        self.redo_button.setEnabled(self.canvas.can_redo())
        self.delete_mark_button.setEnabled(self.canvas.selected_id() is not None)

    def _sync_view_controls(self) -> None:
        z = self.canvas.zoom()
        d = self.canvas.device_pixel_ratio()
        has = self.canvas.shot() is not None and self.canvas.shot().image is not None  # type: ignore[union-attr]
        self.zoom_label.setText(f"{int(round(z * d * 100))}%" if has else "")
        self.fit_button.setChecked(has and self.canvas.is_fit())
        self.actual_button.setChecked(has and not self.canvas.is_fit() and abs(z * d - 1.0) < 1e-3)

    def _on_tool_changed(self, tool) -> None:
        btn = self.tool_buttons.get(Tool(tool))
        if btn is not None and not btn.isChecked():
            btn.setChecked(True)

    def _on_canvas_selection(self) -> None:
        self.marks.select(self.canvas.selected_id())
        self._sync_history_buttons()

    # ==================================================================
    # edits -> model
    # ==================================================================
    def _current_shot(self):
        return self._session.get_shot(self._current_id) if self._current_id else None

    def _on_caption_changed(self) -> None:
        if self._loading:
            return
        shot = self._current_shot()
        if shot is None:
            return
        shot.caption = self.caption_edit.toPlainText()
        self.panel.update_shot(shot.id)
        self.session_changed.emit()

    def _on_role_changed(self, role: Role) -> None:
        shot = self._current_shot()
        if shot is None:
            return
        shot.role = Role(role)
        self.panel.update_shot(shot.id)
        self.session_changed.emit()

    def _set_role_from_key(self, role: Role) -> None:
        if self._current_shot() is None:
            return
        self.role_control.set_role(role, emit=True)

    def _on_annotations_changed(self) -> None:
        shot = self._current_shot()
        self.marks.refresh()
        if shot is not None:
            self.panel.invalidate(shot.id)
        self._sync_history_buttons()
        self.session_changed.emit()

    # ---- session panel requests -----------------------------------------
    def _delete_shot(self, shot_id: str) -> None:
        idx = self._session.index_of(shot_id)
        if idx < 0:
            self.panel.refresh()
            return
        was_current = shot_id == self._current_id
        if was_current:
            self._flush_pending()
        self._session.remove_shot(shot_id)
        self.panel.refresh()
        if was_current:
            shots = self._session.shots
            if shots:
                self.show_shot(shots[min(idx, len(shots) - 1)].id)  # the next one, else the previous
            else:
                self._show_empty()
        self._update_state()
        self.session_changed.emit()

    def _reorder(self, ids: list) -> None:
        try:
            self._session.reorder(list(ids))
        except ValueError:
            self.panel.refresh()  # stale request: show the real order again
            return
        self.panel.refresh()
        self.panel.set_current(self._current_id)
        if self._current_id is not None:
            self._last_index = max(0, self._session.index_of(self._current_id))
        self._update_state()
        self.session_changed.emit()

    # ---- buttons ---------------------------------------------------------
    def _flush_pending(self) -> None:
        """Commit anything still being typed (pin note editor, pin list cell)."""
        self.canvas.commit_pending_edit()
        self.marks.commit_editor()

    def _flush(self) -> None:
        self._flush_pending()
        self.session_changed.emit()

    def _do_next(self) -> None:
        self._flush()
        self.next_requested.emit()

    def _do_next_delayed(self) -> None:
        self._flush()
        self.next_delayed_requested.emit()

    def _do_finish(self) -> None:
        if not self.finish_button.isEnabled():
            return
        self._flush()
        self.finish_requested.emit()

    def _do_finish_to(self) -> None:
        if not self.finish_button.isEnabled():
            return
        self._flush()
        self.finish_to_requested.emit()

    def _on_discard(self) -> None:
        self._flush_pending()
        n = len(self._session.shots)
        what = f"all {n} screenshot{'s' if n != 1 else ''} of this session" if n else "this session"
        answer = QMessageBox.question(
            self,
            "Discard session",
            f"Discard {what}?\n\nNothing has been saved yet, so this cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.discard_session_requested.emit()
