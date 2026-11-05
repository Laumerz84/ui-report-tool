# PyInstaller spec for the packaged UI Report Tool (one folder, no Python needed, no admin needed).
# Build with:  .venv\Scripts\python.exe packaging\build.py
# Output: dist\UIReportTool\ (UIReportTool.exe + uireport-approve.exe + libraries) and a zip of it.
import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = os.path.dirname(SPECPATH)

ua_datas, ua_binaries, ua_hidden = collect_all("uiautomation")  # ships its own UIAutomation DLLs

app = Analysis(
    [os.path.join(ROOT, "launch.pyw")],
    pathex=[ROOT],
    datas=ua_datas,
    binaries=ua_binaries,
    hiddenimports=ua_hidden + collect_submodules("uireport"),
    excludes=["pytest", "tkinter"],
)
approve = Analysis(
    [os.path.join(SPECPATH, "approve_entry.py")],
    pathex=[os.path.join(ROOT, "uireport", "chat")],
    excludes=["PySide6", "pytest", "tkinter"],
)

app_exe = EXE(
    PYZ(app.pure), app.scripts, [],
    exclude_binaries=True, name="UIReportTool", icon=os.path.join(ROOT, "UIReportTool.ico"),
    console=False, upx=False,
)
approve_exe = EXE(
    PYZ(approve.pure), approve.scripts, [],
    exclude_binaries=True, name="uireport-approve", console=True, upx=False,
)
COLLECT(app_exe, app.binaries, app.datas, approve_exe, approve.binaries, approve.datas,
        name="UIReportTool", upx=False)
