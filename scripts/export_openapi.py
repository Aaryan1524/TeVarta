"""Write the versioned OpenAPI artifact from the fixture-backed FastAPI app."""

import json
from pathlib import Path

from app.main import app

target = Path(__file__).resolve().parents[1] / "contracts" / "v1" / "openapi.json"
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
