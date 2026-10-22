"""Entry point for hosted deploys (Embarko): binds to PORT and keeps every company's data in DATA_DIR."""
import os

data_dir = os.environ.get("DATA_DIR")
if data_dir and "DESKHAND_DATA_DIR" not in os.environ:
    os.makedirs(data_dir, exist_ok=True)
    os.environ["DESKHAND_DATA_DIR"] = data_dir  # platform.db and orgs/<company>.db; an older deskhand.db here becomes the demo company

import uvicorn  # noqa: E402
from app.main import app  # noqa: E402

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ["PORT"]))
