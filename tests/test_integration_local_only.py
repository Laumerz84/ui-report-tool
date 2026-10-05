"""Spec: "Everything stays local, with no network calls". Scans the application source (not the tests) with the
AST, so comments and strings cannot cause false alarms. Also guards the tool's own promise not to fake user input."""
from __future__ import annotations

import ast
import os
from pathlib import Path

ROOT = Path(os.path.abspath(__file__)).parent.parent
PKG = ROOT / "uireport"

NETWORK_MODULES = {
    "socket", "ssl", "urllib", "urllib3", "http", "httpx", "requests", "aiohttp", "ftplib", "smtplib", "poplib",
    "imaplib", "telnetlib", "xmlrpc", "websocket", "websockets", "asyncio.streams",
}
NETWORK_QT_NAMES = {"QNetworkAccessManager", "QNetworkRequest", "QTcpSocket", "QUdpSocket", "QTcpServer", "QSslSocket",
                    "QWebSocket", "QWebEngineView", "QLocalSocket"}
INPUT_FAKING_NAMES = {"SendInput", "keybd_event", "mouse_event", "SetCursorPos", "pyautogui", "pynput"}


def _sources():
    files = sorted(PKG.rglob("*.py"))
    assert len(files) > 30
    return files


def _imported_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


def test_the_application_imports_no_network_modules():
    offenders = {}
    for f in _sources():
        bad = _imported_roots(ast.parse(f.read_text(encoding="utf-8"))) & NETWORK_MODULES
        if bad:
            offenders[str(f.relative_to(ROOT))] = sorted(bad)
    assert offenders == {}, offenders


def test_the_application_uses_no_qt_network_or_web_classes():
    offenders = {}
    for f in _sources():
        names = {n.id for n in ast.walk(ast.parse(f.read_text(encoding="utf-8"))) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(ast.parse(f.read_text(encoding="utf-8"))) if isinstance(n, ast.Attribute)}
        names |= {a.name for n in ast.walk(ast.parse(f.read_text(encoding="utf-8"))) if isinstance(n, ast.ImportFrom) for a in n.names}
        bad = names & NETWORK_QT_NAMES
        if bad:
            offenders[str(f.relative_to(ROOT))] = sorted(bad)
    assert offenders == {}, offenders


# The one deliberate exception: on Finish, a Ctrl+V into the Claude desktop app (only when it is
# open and verified to be the foreground window). Nothing else may fake input.
PASTE_MODULE = Path("uireport") / "app" / "claude_paste.py"


def test_the_application_never_fakes_mouse_or_keyboard_input_on_the_desktop():
    offenders = {}
    for f in _sources():
        tree = ast.parse(f.read_text(encoding="utf-8"))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        names |= _imported_roots(tree)
        bad = names & INPUT_FAKING_NAMES
        if f.relative_to(ROOT) == PASTE_MODULE:
            bad -= {"SendInput"}  # its keyboard Ctrl+V only; still no mouse, cursor or other input
        if bad:
            offenders[str(f.relative_to(ROOT))] = sorted(bad)
    assert offenders == {}, offenders


def test_the_paste_exception_sends_only_ctrl_v_and_is_blocked_under_tests():
    text = (ROOT / PASTE_MODULE).read_text(encoding="utf-8")
    calls = [n for n in ast.walk(ast.parse(text)) if isinstance(n, ast.Call)
             and getattr(n.func, "attr", getattr(n.func, "id", None)) == "SendInput"]
    assert len(calls) == 1  # one call site
    assert "VK_RETURN" not in text and "0x0D" not in text  # never presses Enter
    assert os.environ.get("UIREPORT_NO_PASTE") == "1"  # conftest blocks the real paste


def test_only_setup_bat_talks_to_the_internet_and_only_through_pip():
    text = (ROOT / "setup.bat").read_text(encoding="ascii")
    assert "pip install" in text
    for f in ("UIReportTool.bat", "launch.pyw"):
        body = (ROOT / f).read_text(encoding="utf-8")
        assert "pip" not in body and "http://" not in body and "curl" not in body and "Invoke-WebRequest" not in body
