"""Shared pytest setup (owned by the lead; builders must not edit this file).

* Forces Qt's offscreen platform so no window ever appears on the real desktop.
* Provides `test_out` (a per-test folder under .test-output/) and `qapp`.
* Provides `make_image` (synthetic QImage factory) and `make_shot` (a Shot with metadata).
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

TEST_OUTPUT_ROOT = ROOT / ".test-output"


@pytest.fixture
def test_out(request: pytest.FixtureRequest) -> Path:
    """A fresh empty folder under .test-output/<package>/<test name>, where <package> is
    the first word after 'test_' in the test file name (tests/test_capture_grab.py ->
    'capture'). Never touches user folders."""
    stem = Path(str(request.node.fspath)).stem
    tag = re.sub(r"^test_", "", stem).split("_")[0] or "misc"
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", request.node.name)[:80]
    path = TEST_OUTPUT_ROOT / tag / name
    if path.exists():
        import shutil

        shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def make_image():
    """make_image(w, h, color='#FFFFFF') -> opaque QImage filled with one colour."""
    from PySide6.QtGui import QColor, QImage

    def _make(w: int = 200, h: int = 120, color: str = "#FFFFFF") -> "QImage":
        img = QImage(w, h, QImage.Format.Format_ARGB32)
        img.fill(QColor(color))
        return img

    return _make


@pytest.fixture
def make_shot(make_image):
    """make_shot(w=200, h=120, scale_percent=100, **fields) -> Shot with an image + metadata."""
    from uireport.geometry import IntRect, dpi_from_scale_percent
    from uireport.models import CaptureMode, MonitorMeta, Shot, WindowMeta

    def _make(w: int = 200, h: int = 120, scale_percent: int = 100, color: str = "#FFFFFF", **kw) -> Shot:
        mon = MonitorMeta(
            index=1,
            name=r"\\.\DISPLAY1",
            is_primary=True,
            rect=IntRect(0, 0, 1920, 1080),
            dpi=dpi_from_scale_percent(scale_percent),
        )
        shot = Shot(
            capture_mode=kw.pop("capture_mode", CaptureMode.NORMAL),
            capture_rect=IntRect(100, 50, w, h),
            monitor=mon,
            window=WindowMeta(
                title="Demo - Example App",
                process_name="example.exe",
                exe_path=r"C:\Program Files\Example\example.exe",
                pid=1234,
                rect=IntRect(80, 40, 1200, 800),
                dpi=mon.dpi,
            ),
            **kw,
        )
        shot.set_image(make_image(w, h, color))
        return shot

    return _make
