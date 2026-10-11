"""Helper for show_claude.py: a plain top-most window with big text, in its own process (like any app the
user would capture). Prints its screen rectangle as JSON once shown, then stays until killed."""
import json
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QLabel

app = QApplication(sys.argv)
text, x = sys.argv[1], int(sys.argv[2])
w = QLabel(text)
w.setWindowFlags(Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.FramelessWindowHint)
w.setWindowTitle(text)
w.setStyleSheet("background: white; color: black; padding: 24px;")
w.setFont(QFont("Segoe UI", 40, QFont.Weight.Bold))
w.adjustSize()
w.move(x, 200)
w.show()


def report() -> None:
    g = w.frameGeometry()
    print(json.dumps([g.left(), g.top(), g.right(), g.bottom()]), flush=True)


QTimer.singleShot(600, report)
app.exec()
