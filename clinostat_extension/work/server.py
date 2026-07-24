"""Non-operational fixture matching the known Pi route structure."""

from fastapi import FastAPI, WebSocket

app = FastAPI()


@app.get("/api/control/status")
async def control_status():
    return {"running": False, "loop_timing_ms": None}


@app.websocket("/ws/control")
async def control_stream(websocket: WebSocket):
    return None
