"""Build the packaged UI Report Tool and zip it for a GitHub release.

    .venv\\Scripts\\python.exe packaging\\build.py      (needs: pip install pyinstaller)

Writes dist\\UIReportTool\\ and dist\\UIReportTool-<version>-windows.zip (build\\ is scratch).
"""
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from uireport import __version__  # noqa: E402

README = """UI Report Tool {v}

1. Unzip this folder anywhere you like (Downloads, Documents...). No admin rights or Python needed.
2. Double-click UIReportTool.exe. Its icon appears in the system tray (bottom right, maybe under the ^ arrow).
3. Ctrl+Alt+S captures a screenshot. Right-click the tray icon for Settings
   (there you can also turn on "Start with Windows").

Show Claude (Ctrl+Alt+Shift+C) needs Claude Code installed and signed in: https://claude.com/claude-code
Settings live in %APPDATA%\\UIReportTool. To remove the tool: Quit it from the tray, then delete this folder.
Source and help: https://github.com/Laumerz84/ui-report-tool
"""


def main() -> None:
    dist, work = os.path.join(ROOT, "dist"), os.path.join(ROOT, "build")
    subprocess.check_call([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                           "--distpath", dist, "--workpath", work,
                           os.path.join(ROOT, "packaging", "uireport.spec")])
    folder = os.path.join(dist, "UIReportTool")
    with open(os.path.join(folder, "README.txt"), "w", encoding="utf-8") as f:
        f.write(README.format(v=__version__))
    base = os.path.join(dist, f"UIReportTool-{__version__}-windows")
    zip_path = shutil.make_archive(base, "zip", dist, "UIReportTool")
    print(f"built {folder}\nzipped {zip_path} ({os.path.getsize(zip_path) / 1e6:.0f} MB)")


if __name__ == "__main__":
    main()
