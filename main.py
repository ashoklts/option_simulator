from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from pymongo import MongoClient
from typing import List, Optional
from pydantic import BaseModel
from datetime import datetime, timedelta
import json

# Mini Strangle engine
from mini_strangle.api_server import router as mini_strangle_router

# Initialize FastAPI
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include Mini Strangle engine routes under /engine prefix
app.include_router(mini_strangle_router, prefix="/engine")

# MongoDB connection
MONGO_URI = "mongodb://localhost:27017/"

client = MongoClient(MONGO_URI)

db = client["stock_data"]
collection = db["option_chain"]


@app.get("/get-option-chain")
def get_option_chain(timestamp: str = Query(...)):

    try:

        # Query MongoDB
        print({"timestamp": timestamp})
        cursor = collection.find(
            {"timestamp": timestamp},
            {"_id": 0}   # Remove _id field
        )

        data = list(cursor)

        return {
            "status": "success",
            "timestamp": timestamp,
            "count": len(data),
            "data": data
        }

    except Exception as e:

        return {
            "status": "error",
            "message": str(e)
        }


# ─────────────────────────────────────────────────────────────────────────────
# /next-adjustment-deduct
# ─────────────────────────────────────────────────────────────────────────────

def _ts_date(ts: str) -> str:
    return ts.replace("T", " ").split(" ")[0]


def _ts_time(ts: str) -> str:
    """Return HH:MM from an ISO/space-separated timestamp."""
    return ts.replace("T", " ").split(" ")[-1][:5]


def _ts_subtract_minutes(ts: str, minutes: int) -> str:
    fmt = "%Y-%m-%dT%H:%M:%S" if "T" in ts else "%Y-%m-%d %H:%M:%S"
    try:
        dt = datetime.strptime(ts[:19], fmt)
        return (dt - timedelta(minutes=minutes)).strftime(fmt)
    except Exception:
        return ts


def _resolve_otm_index(fifth_ce_prem: float) -> int:
    """Mirror of the frontend resolveTargetOtmIndex logic."""
    if fifth_ce_prem < 100:  return 5
    if fifth_ce_prem <= 160: return 5
    if fifth_ce_prem <= 270: return 6
    if fifth_ce_prem <= 370: return 7
    if fifth_ce_prem <= 470: return 8
    return 9


def _select_expiry(expiries: list, expiry_type: str, date_str: str) -> str:
    if not expiries:
        return ""
    et = expiry_type.lower()
    if et == "next_week":
        return expiries[1] if len(expiries) > 1 else expiries[0]
    if et == "monthly_expiry":
        year, month = int(date_str[:4]), int(date_str[5:7])
        monthly = [e for e in expiries if int(e[:4]) == year and int(e[5:7]) == month]
        return monthly[-1] if monthly else expiries[-1]
    return expiries[0]  # current_week — first expiry


class OpenPosition(BaseModel):
    leg_type:      str    # 'Buy' or 'Sell'
    option_type:   str    # 'CE' or 'PE'
    strike:        float
    expiry:        str
    entry_premium: float
    quantity:      int


class NextAdjDeductRequest(BaseModel):
    timestamp: str
    upper_adjustment_point: Optional[float] = None
    lower_adjustment_point: Optional[float] = None
    lot: int = 2
    lot_size: int = 65
    timeframe: str = "1m"
    strategy_type: str = "mini_strangle"
    expiry_type: str = "current_week"
    stoploss_status: int = 0
    stoploss_type: Optional[int] = 1
    stoploss_value: Optional[float] = None
    target_status: int = 0
    target_type: Optional[int] = 1
    target_value: Optional[float] = None
    trailing_sl_status: int = 0
    trailing_sl_type: Optional[int] = 1
    trailing_sl_x: Optional[float] = None
    trailing_sl_y: Optional[float] = None
    position_end_time: str = "15:26"
    open_positions: List[OpenPosition] = []
    closed_positions_pnl: float = 0.0


@app.post("/next-adjustment-deduct")
async def next_adjustment_deduct(req: NextAdjDeductRequest):
    try:
        from mini_strangle.risk_manager import RiskManager, RiskConfig

        ts_date = _ts_date(req.timestamp)
        ts_time = _ts_time(req.timestamp)

        # ── 1. Collect timestamps for this trading day in range ───────────────
        all_ts = sorted(collection.distinct("timestamp"))
        day_ts = [
            t for t in all_ts
            if _ts_date(t) == ts_date
            and _ts_time(t) >= ts_time
            and _ts_time(t) <= req.position_end_time
        ]

        if not day_ts:
            return {
                "event_triggered": False,
                "message": "No market data available for this timestamp",
                "position_end_timestamp": f"{ts_date}T{req.position_end_time}:00",
            }

        # ── 2. Set up RiskManager ─────────────────────────────────────────────
        # Capital base for percentage-based SL/Target: ₹2,00,000 per lot
        capital_base = 200_000 * req.lot

        risk_config = RiskConfig(
            stoploss_status=req.stoploss_status,
            stoploss_type=req.stoploss_type,
            stoploss_value=req.stoploss_value,
            target_status=req.target_status,
            target_type=req.target_type,
            target_value=req.target_value,
            trailing_sl_status=req.trailing_sl_status,
            trailing_sl_x=req.trailing_sl_x,
            trailing_sl_y=req.trailing_sl_y,
        )
        risk_mgr = RiskManager(risk_config, initial_premium=capital_base)

        # ── 3. Scan ticks forward ─────────────────────────────────────────────
        for scan_ts in day_ts:
            scan_docs = list(collection.find({"timestamp": scan_ts}, {"_id": 0}))

            spot = next(
                (float(d["spot_price"]) for d in scan_docs if d.get("spot_price")), None
            )
            if spot is None:
                continue

            # ── Adjustment point check (spot-price based) ─────────────────────
            if req.upper_adjustment_point is not None and spot >= req.upper_adjustment_point:
                return {
                    "event_triggered": True,
                    "event_type": "upper_adjustment_hit",
                    "trigger_timestamp": scan_ts,
                    "validation_start_timestamp": _ts_subtract_minutes(scan_ts, 5),
                    "spot_price": spot,
                }

            if req.lower_adjustment_point is not None and spot <= req.lower_adjustment_point:
                return {
                    "event_triggered": True,
                    "event_type": "lower_adjustment_hit",
                    "trigger_timestamp": scan_ts,
                    "validation_start_timestamp": _ts_subtract_minutes(scan_ts, 5),
                    "spot_price": spot,
                }

            # ── Open positions PnL from actual UI positions ───────────────────
            open_pnl = 0.0
            for pos in req.open_positions:
                # Normalize expiry: MongoDB may store as datetime object or string
                cur_price = next(
                    (
                        float(d.get("close", 0))
                        for d in scan_docs
                        if abs(float(d.get("strike", 0)) - pos.strike) < 0.01
                        and d.get("type") == pos.option_type
                        and str(d.get("expiry", "")).startswith(pos.expiry)
                    ),
                    pos.entry_premium,  # fallback: no price movement
                )
                leg_pnl = (
                    (cur_price - pos.entry_premium) * pos.quantity
                    if pos.leg_type == "Buy"
                    else (pos.entry_premium - cur_price) * pos.quantity
                )
                open_pnl += leg_pnl

            # Total PnL = open positions PnL + accumulated closed PnL
            total_pnl = open_pnl + req.closed_positions_pnl

            # ── Risk checks ───────────────────────────────────────────────────
            if risk_mgr.check_stoploss(total_pnl):
                return {
                    "event_triggered": True,
                    "event_type": "stoploss_hit",
                    "trigger_timestamp": scan_ts,
                    "validation_start_timestamp": _ts_subtract_minutes(scan_ts, 5),
                    "spot_price": spot,
                    "total_pnl": round(total_pnl, 2),
                }

            if risk_mgr.check_target(total_pnl):
                return {
                    "event_triggered": True,
                    "event_type": "target_hit",
                    "trigger_timestamp": scan_ts,
                    "validation_start_timestamp": _ts_subtract_minutes(scan_ts, 5),
                    "spot_price": spot,
                    "total_pnl": round(total_pnl, 2),
                }

            if risk_mgr.check_trailing_sl(total_pnl):
                return {
                    "event_triggered": True,
                    "event_type": "trailing_sl_hit",
                    "trigger_timestamp": scan_ts,
                    "validation_start_timestamp": _ts_subtract_minutes(scan_ts, 5),
                    "spot_price": spot,
                    "total_pnl": round(total_pnl, 2),
                }

        # ── 4. No event triggered ─────────────────────────────────────────────
        return {
            "event_triggered": False,
            "message": "No event triggered",
            "position_end_timestamp": f"{ts_date}T{req.position_end_time}:00",
        }

    except Exception as e:
        return {"status": "error", "message": str(e)}