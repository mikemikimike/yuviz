"""python -m services.realestate — the port from REALESTATE_SERVICE_URL (default 8710), LISTEN_HOST from the
environment (default 127.0.0.1)."""

import os
from urllib.parse import urlparse

import uvicorn

if __name__ == "__main__":
    port = urlparse(os.environ.get("REALESTATE_SERVICE_URL", "")).port or 8710
    uvicorn.run("services.realestate.app:app", host=os.environ.get("LISTEN_HOST", "127.0.0.1"), port=port)
