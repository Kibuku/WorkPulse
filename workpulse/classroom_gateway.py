"""Minimal LAN gateway for enrolled WorkPulse Classroom devices.

This process intentionally exposes no personal dashboard or WorkPulse memory.
Only health, enrolment, policy delivery and device heartbeat routes exist.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn

from workpulse.core import classroom


app = FastAPI(title="WorkPulse Classroom Gateway", docs_url=None, redoc_url=None)


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    return header[7:].strip() if header.lower().startswith("bearer ") else ""


@app.get("/health")
def health():
    return {"ok": True, "service": "workpulse-classroom-gateway"}


@app.post("/api/v2/classroom/agent/enroll")
async def enroll(request: Request):
    payload = await request.json()
    try:
        return classroom.enroll_device(
            str(payload.get("code") or ""),
            str(payload.get("name") or ""),
            str(payload.get("platform") or "unknown"),
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.get("/api/v2/classroom/agent/policy")
def policy(request: Request):
    if not classroom.authenticate_device(_bearer(request)):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return classroom.agent_policy()


@app.post("/api/v2/classroom/agent/heartbeat")
async def heartbeat(request: Request):
    payload = await request.json()
    try:
        return classroom.device_heartbeat(
            _bearer(request), payload.get("signal") or {}
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=401)


def run(host: str = "0.0.0.0", port: int = 5722):
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    run()
