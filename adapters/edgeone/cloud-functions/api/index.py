import sys

from fastapi import FastAPI

try:
    import mijia_assistant
except ImportError:
    from api import mijia_assistant

    sys.modules["mijia_assistant"] = mijia_assistant

from api.mijia_agent.app import create_lifespan, register_routes
from api.mijia_agent.config import Settings

settings = Settings.from_env()
lifespan = create_lifespan(settings)
app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
register_routes(app, settings)
