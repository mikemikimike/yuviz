"""python -m services.clinic — PORT / LISTEN_HOST from the environment (default 127.0.0.1:8700)."""

import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "services.clinic.app:app",
        host=os.environ.get("LISTEN_HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8700")),
    )
