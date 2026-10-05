"""Application factory and operational health contract."""

from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel

from sitesense_api.config import Settings


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    service: Literal["sitesense-api"] = "sitesense-api"


def create_app(settings: Settings | None = None) -> FastAPI:
    configuration = settings if settings is not None else Settings()
    documentation_enabled = configuration.environment != "production"
    app = FastAPI(
        title="SiteSense API",
        description="Weather-aware retail location assessment API.",
        version="0.1.0",
        docs_url="/docs" if documentation_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if documentation_enabled else None,
    )

    @app.get("/healthz", response_model=HealthResponse, include_in_schema=False)
    @app.get(
        "/api/v1/health",
        response_model=HealthResponse,
        operation_id="getHealth",
        tags=["health"],
        summary="Check API process health",
    )
    async def health() -> HealthResponse:
        return HealthResponse()

    return app
