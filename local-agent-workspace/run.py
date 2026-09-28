"""Start the built app with the Python environment used to invoke this file."""
import argparse
import os
import sys
from pathlib import Path

import uvicorn

root = Path(__file__).resolve().parent
sys.path.insert(0, str(root / "backend"))
os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Local agent workspace")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()
    uvicorn.run("local_agent.api:create_app", factory=True, host="127.0.0.1", port=args.port, reload=args.reload, access_log=False)
