import sys
from pathlib import Path

from fastapi import FastAPI

# EdgeOne loads this file as ``api/index.py`` with the parent directory on
# sys.path.  The copied application packages live beside this file and use
# top-level imports, so make that directory importable before loading them.
API_ROOT = str(Path(__file__).resolve().parent)
if API_ROOT not in sys.path:
    sys.path.insert(0, API_ROOT)

from mijia_agent.app import create_lifespan, register_routes
from mijia_agent.config import Settings

settings = Settings.from_env()
lifespan = create_lifespan(settings)
app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
register_routes(app, settings)
