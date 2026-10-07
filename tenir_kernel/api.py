"""Optional, authenticated FastAPI wrapper for the deterministic kernel."""

import hmac
import os
from typing import Any


from .kernel import TenirKernel


def create_app():
    try:
        from fastapi import FastAPI, Header, HTTPException
        from fastapi.concurrency import run_in_threadpool
        from pydantic import BaseModel, ConfigDict
    except ImportError as exc:  # pragma: no cover - depends on optional install
        raise RuntimeError("Install the API extra: pip install -e '.[api]'") from exc

    class AdjudicationPayload(BaseModel):
        model_config = ConfigDict(extra="forbid")
        runtime_object: dict[str, Any]

    app = FastAPI(
        title="TENIR Kernel API",
        version="1.2.0",
        description="Authenticated TENIR 2.0 schema 1.2 runtime adjudication.",
    )
    app.state.kernel = TenirKernel()
    app.state.mode = os.getenv("TENIR_MODE", "SHADOW_PASSIVE").upper()
    if app.state.mode not in {"SHADOW_OFF", "SHADOW_PASSIVE", "SHADOW_CRITICAL", "ENFORCE"}:
        raise RuntimeError("TENIR_MODE must be SHADOW_OFF, SHADOW_PASSIVE, SHADOW_CRITICAL, or ENFORCE")

    async def authenticate(authorization: str | None = Header(default=None)) -> None:
        expected = os.getenv("TENIR_API_TOKEN", "")
        if len(expected) < 32:
            raise HTTPException(status_code=503, detail="TENIR API token is not configured")
        scheme, _, supplied = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(supplied, expected):
            raise HTTPException(status_code=401, detail="Bearer token required")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/v1/adjudicate")
    async def adjudicate(
        payload: AdjudicationPayload,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        await authenticate(authorization)
        try:
            return await run_in_threadpool(
                app.state.kernel.adjudicate_runtime_object,
                payload.runtime_object,
                mode=app.state.mode,
            )
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return app


app = create_app()
