"""Entry point for hosted deploys (Embarko): binds to PORT, stores data in DATA_DIR."""
import os

data_dir = os.environ.get("DATA_DIR")
if data_dir and "DESKHAND_DB" not in os.environ:
    os.makedirs(data_dir, exist_ok=True)
    os.environ["DESKHAND_DB"] = os.path.join(data_dir, "deskhand.db")

import uvicorn  # noqa: E402
from app.main import app  # noqa: E402

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ["PORT"]))
