"""
streaming_controller.py
-----------------------
Manages the Server-Sent Events (SSE) stream for a single engine session.

Usage:
  controller = StreamingController()
  asyncio.create_task(engine.run(controller))   # engine pushes events
  return StreamingResponse(controller.stream(), media_type="text/event-stream")
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import AsyncGenerator, Optional

logger = logging.getLogger(__name__)

_HEARTBEAT_INTERVAL = 5.0   # seconds between keep-alive pings
_QUEUE_TIMEOUT = 1.0        # seconds to wait before sending heartbeat

# Events that are ALWAYS logged regardless of frequency
_ALWAYS_LOG_EVENTS = {
    "started", "stopped", "error",
    "positions_opened", "positions_closed",
    "hedge_opened", "hedge_closed",
    "adjustment_triggered", "otm_adjustment_triggered",
    "reentry_scheduled", "event_reentry_scheduled", "next_expiry_scheduled",
    "stoploss_hit", "target_hit", "trailing_sl_hit",
    "minute_pnl",
    "expiry_exit",
}


class StreamingController:
    def __init__(self, position_start_time: str = "09:15") -> None:
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._stopped: bool = False

        # Hourly monitor log filter
        # Extract the minute component from position_start_time (e.g. "09:20" → "20")
        self._log_minute: str = position_start_time.split(":")[1]  # "20"
        self._last_monitor_log_hhmm: str = ""  # tracks last logged HH:MM

    # ------------------------------------------------------------------
    # Push side (called by StrategyEngine)
    # ------------------------------------------------------------------

    async def send(self, event_type: str, data: dict) -> None:
        """
        Serialize and queue one SSE event.

        Logging rules:
          - All non-monitor events → always logged at INFO level.
          - monitor events         → logged only once per hour,
                                     at the minute matching position_start_time.
            Example: position_start_time=09:20
              Logged at: 09:20, 10:20, 11:20, 12:20 …
              Skipped  : 09:21, 09:22 … 10:19, 10:21 …

        The SSE stream always receives every event regardless.
        """
        payload = json.dumps(
            {
                "event": event_type,
                "ts": datetime.now().isoformat(timespec="milliseconds"),
                "data": data,
            }
        )
        await self._queue.put(payload)

        # Logging — monitor is hourly; all others always logged
        if event_type == "monitor":
            self._maybe_log_monitor(data)
        elif event_type in _ALWAYS_LOG_EVENTS:
            logger.info(f"[{event_type}] {self._summarise(event_type, data)}")
        else:
            logger.debug(f"[stream] queued event={event_type}")

    def _maybe_log_monitor(self, data: dict) -> None:
        """Log a monitor event only when minute == position_start_time's minute."""
        ts: str = data.get("timestamp", "")
        if not ts:
            return

        # Extract HH:MM from the candle timestamp (not wall-clock time)
        sep = "T" if "T" in ts else " "
        hhmm = ts.split(sep)[-1][:5]          # "HH:MM"
        current_minute = hhmm.split(":")[1]    # "MM"

        if current_minute != self._log_minute:
            return   # not the correct minute — skip

        if hhmm == self._last_monitor_log_hhmm:
            return   # already logged this exact HH:MM — skip duplicate

        self._last_monitor_log_hhmm = hhmm
        spot   = data.get("spot", "?")
        pnl    = data.get("current_pnl", data.get("pnl", "?"))
        expiry = data.get("current_expiry", "?")
        logger.info(
            f"[monitor] {ts} | spot={spot} | pnl={pnl} | expiry={expiry}"
        )

    @staticmethod
    def _summarise(event_type: str, data: dict) -> str:
        """Build a concise one-line log string for important events."""
        ts = data.get("timestamp", "")
        if event_type == "positions_opened":
            return (
                f"{ts} | CE={data.get('ce_strike')}@{data.get('ce_entry_price')} "
                f"PE={data.get('pe_strike')}@{data.get('pe_entry_price')} "
                f"expiry={data.get('expiry')}"
            )
        if event_type == "positions_closed":
            return (
                f"{ts} | reason={data.get('reason')} "
                f"cycle_pnl={data.get('cycle_pnl', data.get('pnl', '?'))} "
                f"cumulative_pnl={data.get('cumulative_pnl', '?')}"
            )
        if event_type == "adjustment_triggered":
            return (
                f"{ts} | side={data.get('side')} spot={data.get('spot_price')} "
                f"upper={data.get('upper_adjustment_price')} "
                f"lower={data.get('lower_adjustment_price')}"
            )
        if event_type in ("stoploss_hit", "target_hit", "trailing_sl_hit"):
            return (
                f"{ts} | cycle_pnl={data.get('cycle_pnl', '?')} "
                f"cumulative_pnl={data.get('cumulative_pnl', '?')}"
            )
        if event_type == "minute_pnl":
            return (
                f"{ts} | expiry={data.get('current_expiry', '?')} "
                f"expiry_net={data.get('expiry_net_pnl', data.get('pnl_with_charges', '?'))} "
                f"expiry_charges={data.get('expiry_total_charges', data.get('total_charges', '?'))}"
            )
        if event_type == "next_expiry_scheduled":
            return f"{ts} | {data.get('previous_expiry')} → {data.get('next_expiry')}"
        if event_type == "event_reentry_scheduled":
            return (
                f"{ts} | reason={data.get('reason')} mode={data.get('event_hit_position_status')} "
                f"scheduled_for={data.get('scheduled_for')} expiry={data.get('target_expiry')}"
            )
        if event_type == "hedge_opened":
            return (
                f"{ts} | CE={data.get('ce_hedge_strike')}@{data.get('ce_hedge_price')} "
                f"PE={data.get('pe_hedge_strike')}@{data.get('pe_hedge_price')}"
            )
        return f"{ts} | {data}"

    def stop(self) -> None:
        """Signal that no more events will be produced."""
        self._stopped = True

    # ------------------------------------------------------------------
    # Pull side (consumed by FastAPI StreamingResponse)
    # ------------------------------------------------------------------

    async def stream(self) -> AsyncGenerator[str, None]:
        """
        Yields SSE-formatted strings.
        Sends a heartbeat comment every _HEARTBEAT_INTERVAL seconds so
        the HTTP connection stays alive during quiet periods.
        """
        while True:
            try:
                message = await asyncio.wait_for(
                    self._queue.get(), timeout=_QUEUE_TIMEOUT
                )
                yield f"data: {message}\n\n"
            except asyncio.TimeoutError:
                if self._stopped and self._queue.empty():
                    break
                # Keep-alive comment (ignored by SSE parsers)
                yield f": heartbeat {datetime.now().isoformat(timespec='seconds')}\n\n"
            except Exception as exc:
                logger.error(f"[stream] unexpected error: {exc}")
                break
