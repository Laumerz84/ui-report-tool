"""Entry point of uireport-approve.exe, the console helper the packaged app runs as Show Claude's
approval server (a windowed exe has no usable stdin/stdout). Same code as uireport/chat/approve_mcp.py."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "uireport", "chat"))

import approve_mcp  # noqa: E402

if __name__ == "__main__":
    approve_mcp.main()
