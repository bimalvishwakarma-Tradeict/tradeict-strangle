# run_local_server.py — Start FastAPI locally for Backtest Local Mode UI

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.getcwd())

import uvicorn


if __name__ == "__main__":
    uvicorn.run(
        "backend.main:app",
        host="127.0.0.1",
        port=8000,
        reload=False,
    )
