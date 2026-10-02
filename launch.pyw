"""Double-click launcher for UI Report Tool.

The .pyw extension makes Windows run this with pythonw.exe, so no console window appears.
It puts this folder on sys.path (so ``uireport`` is importable from anywhere) and starts the tray app.
Errors go to %APPDATA%\\UIReportTool\\uireport.log and, for unexpected ones, a message box.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from uireport.app.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
