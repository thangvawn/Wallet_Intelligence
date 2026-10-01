"""Entrypoint for the deterministic CT wallet rank dashboard."""
import sys
import os
import uvicorn
from pathlib import Path

# Add project root to sys.path
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

if __name__ == "__main__":
    # 8080 is used by another local project on this workstation.
    port = int(os.environ.get("CT_WALLET_PORT", "8081"))
    print(f"================================================================")
    print(f"🚀 CT WALLET RANK ENGINE LAUNCHING")
    print(f"📡 Dashboard available at: http://127.0.0.1:{port}")
    print(f"API Docs available at: http://127.0.0.1:{port}/docs")
    print(f"================================================================")
    uvicorn.run("app.server:app", host="127.0.0.1", port=port, reload=False)
