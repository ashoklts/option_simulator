"""
api_server.py
-------------
APIRouter for the Mini Strangle backtesting engine.
Included in main.py with prefix="/engine".

Endpoints:
  POST /engine/mini-strangle/start           → starts engine, returns SSE stream
  POST /engine/mini-strangle/stop/{id}       → stops a running session
  GET  /engine/mini-strangle/sessions        → list active session IDs
  GET  /engine/health                        → health check

SSE Event Types emitted on the stream:
  started              → engine initialised
  positions_opened     → CE + PE sells placed, adjustment levels included
  monitor              → per-tick update (spot, ATM, PnL, risk status …)
  adjustment_triggered → spot hit upper or lower adjustment level
  otm_adjustment_triggered → sold strike moved within OTM shift distance of ATM
  positions_closed     → sells closed before re-entry
  reentry_scheduled    → re-entry delay queued
  event_reentry_scheduled → SL/target/TSL continuation queued
  hedge_opened         → hedge BUY positions placed
  hedge_closed         → hedge positions closed
  stoploss_hit         → stop-loss exit
  target_hit           → profit target exit
  trailing_sl_hit      → trailing stop-loss exit
  stopped              → engine has finished
  error                → unrecoverable error
"""

import asyncio
import logging
import uuid
from typing import Dict

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from .models import MiniStrangleRequest
from .strategy_engine import StrategyEngine
from .streaming_controller import StreamingController

logger = logging.getLogger(__name__)

router = APIRouter()

# Active engine sessions  {session_id → StrategyEngine}
_sessions: Dict[str, StrategyEngine] = {}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/mini-strangle/start", summary="Start a Mini Strangle backtesting session")
async def start_mini_strangle(request: MiniStrangleRequest) -> StreamingResponse:
    """
    Starts the engine and streams results back as Server-Sent Events.

    Each SSE message is a JSON object:
    ```json
    {"event": "<event_type>", "ts": "<ISO timestamp>", "data": { … }}
    ```

    The session ID is returned in the `X-Session-ID` response header.
    Use it to call `/engine/mini-strangle/stop/{session_id}` to halt early.
    """
    session_id = str(uuid.uuid4())
    stream = StreamingController(position_start_time=request.position_start_time)
    engine = StrategyEngine(request, stream)

    _sessions[session_id] = engine

    asyncio.create_task(_run_session(session_id, engine))

    logger.info(
        f"Session {session_id} started | "
        f"{request.backtest_start_date} → {request.backtest_end_date}"
    )

    return StreamingResponse(
        stream.stream(),
        media_type="text/event-stream",
        headers={
            "X-Session-ID": session_id,
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post(
    "/mini-strangle/stop/{session_id}",
    summary="Stop an active Mini Strangle session",
)
async def stop_mini_strangle(session_id: str) -> dict:
    engine = _sessions.get(session_id)
    if not engine:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    engine.stop()
    _sessions.pop(session_id, None)
    logger.info(f"Session {session_id} stopped via API")
    return {"status": "stopped", "session_id": session_id}


@router.get("/mini-strangle/sessions", summary="List all active session IDs")
async def list_sessions() -> dict:
    return {"active_sessions": list(_sessions.keys()), "count": len(_sessions)}


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _run_session(session_id: str, engine: StrategyEngine) -> None:
    """Wrapper that removes the session from the registry when the engine finishes."""
    try:
        await engine.run()
    finally:
        _sessions.pop(session_id, None)
        logger.info(f"Session {session_id} removed from registry")
