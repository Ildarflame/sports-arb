from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates

TEMPLATES_DIR = Path(__file__).parent / "templates"

app = FastAPI(title="Sports Arbitrage Dashboard", version="0.1.0")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Make Platform enum available in templates
from src.models import Platform  # noqa: E402
templates.env.globals["Platform"] = Platform

# Custom Jinja2 filter: parse JSON string to dict
def _from_json(value):
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {}

templates.env.filters["from_json"] = _from_json


def setup_routes() -> None:
    from src.web.routes import router
    app.include_router(router)
