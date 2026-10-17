"""Start the built app with the Python environment used to invoke this file."""
import argparse
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

root = Path(__file__).resolve().parent
sys.path.insert(0, str(root / "backend"))
os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")

OPEN_WAIT_SECONDS = 10


def accepting(port):
    """Whether something on 127.0.0.1:port accepts connections."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def desktop_browser(environ=None, platform=None):
    """Whether --open can start a graphical browser: on macOS and Windows, or with a display (X11 or Wayland) or a
    browser named in BROWSER. Without them Python's webbrowser runs a text browser (w3m, lynx…) in this terminal,
    which then fights the server's log output and waits until that browser is closed."""
    environ = os.environ if environ is None else environ
    platform = sys.platform if platform is None else platform
    return platform in ("darwin", "win32") or any(environ.get(name) for name in ("DISPLAY", "WAYLAND_DISPLAY", "BROWSER"))


def open_when_ready(port, timeout=OPEN_WAIT_SECONDS, opener=None):
    """--open: open the app in the default browser once 127.0.0.1:port accepts connections (checked every 0.1 s
    for at most timeout seconds). True when the browser was asked to open it."""
    deadline = time.monotonic() + timeout
    while not accepting(port):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)
    try:
        return bool((opener or webbrowser.open)(f"http://127.0.0.1:{port}/"))
    except webbrowser.Error:
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local agent workspace")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--reload", action="store_true")
    parser.add_argument("--open", action="store_true", help="open the app in your browser once it is ready")
    args = parser.parse_args(argv)
    # A port already in use is another program's (the start fails and says so): no browser is opened for it.
    if args.open and not accepting(args.port):
        if desktop_browser():
            threading.Thread(target=open_when_ready, args=(args.port,), daemon=True).start()
        else:
            print(f"No desktop to open a browser on: open http://127.0.0.1:{args.port}/ in your browser.", flush=True)
    from local_agent import serve
    serve.run("local_agent.api:create_app", args.port, reload=args.reload)


if __name__ == "__main__":
    main()
