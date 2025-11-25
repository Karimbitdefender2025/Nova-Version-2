import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Literal, Optional
from fastapi.middleware.cors import CORSMiddleware
from keep_alive import keep_alive
keep_alive()


import websockets
from fastapi import FastAPI
from pydantic import BaseModel

# =========================
#  Types / Models
# =========================

SignalStrength = Literal["weak", "good", "strong", "very strong"]
SideType = Literal["long", "short", "neutral"]


class SymbolStats(BaseModel):
    # بيانات أساسية من Binance
    symbol: str
    last_price: float
    price_change_percent: float   # 24h %
    quote_volume_24h: float
    best_bid: float
    best_ask: float

    # مشتقات سعر/سبريد
    spread_percent: float
    strength: SignalStrength
    side: SideType

    # فوليوم لكل إطار زمني (Quote volume)
    vol_1m: float
    vol_5m: float
    vol_15m: float
    vol_60m: float

    # Net volume (buy - sell) لكل إطار
    net_1m: float
    net_5m: float
    net_15m: float
    net_60m: float

    # مقارنة الدقيقة بمتوسط 60 دقيقة
    vol_1m_vs_60m_avg: float

    # عدد الـ Pings في آخر 24 ساعة
    pings_24h: int


@dataclass
class MinuteBucket:
    ts_minute: int
    volume: float = 0.0       # quote volume
    buy_volume: float = 0.0   # quote volume in up moves
    sell_volume: float = 0.0  # quote volume in down moves


@dataclass
class SymbolState:
    last_price: float = 0.0
    last_quote_24h: float = 0.0
    minute_buckets: Deque[MinuteBucket] = field(
        default_factory=lambda: deque(maxlen=60)
    )
    ping_timestamps: Deque[int] = field(default_factory=deque)


# =========================
#  Global State
# =========================



app = FastAPI(title="Nova Scanner Backend", version="0.3.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],      # تقدر بعدين تستبدلها بالأوريجن الحقيقي
    allow_credentials=False,  # <<< دي المهمّة
    allow_methods=["*"],
    allow_headers=["*"],
)



BINANCE_WS_URL = "wss://stream.binance.com:9443/ws/!ticker@arr"

# internal state
_symbol_state: Dict[str, SymbolState] = {}

# exposed API state
symbol_data: Dict[str, SymbolStats] = {}


# =========================
#  Helper functions
# =========================

def _update_minute_buckets(state: SymbolState, delta_quote: float, direction: int) -> None:
    """تحديث bucket الدقيقة الحالية بفوليوم جديد."""
    now = int(time.time())
    current_min = now // 60

    buckets = state.minute_buckets
    if not buckets or buckets[-1].ts_minute != current_min:
        buckets.append(MinuteBucket(ts_minute=current_min))

    bucket = buckets[-1]
    bucket.volume += max(0.0, delta_quote)

    if direction > 0:
        bucket.buy_volume += max(0.0, delta_quote)
    elif direction < 0:
        bucket.sell_volume += max(0.0, delta_quote)


def _compute_volume_windows(state: SymbolState) -> Dict[str, float]:
    """حساب vol_1m / 5m / 15m / 60m و net_… لكل Symbol."""
    buckets = list(state.minute_buckets)
    if not buckets:
        return {
            "vol_1m": 0.0,
            "vol_5m": 0.0,
            "vol_15m": 0.0,
            "vol_60m": 0.0,
            "net_1m": 0.0,
            "net_5m": 0.0,
            "net_15m": 0.0,
            "net_60m": 0.0,
            "vol_1m_vs_60m_avg": 0.0,
        }

    def sum_last(n: int, attr: str) -> float:
        slice_buckets = buckets[-n:] if len(buckets) >= n else buckets
        return sum(getattr(b, attr) for b in slice_buckets)

    vol_1m = buckets[-1].volume
    vol_5m = sum_last(5, "volume")
    vol_15m = sum_last(15, "volume")
    vol_60m = sum_last(60, "volume")

    net_1m = buckets[-1].buy_volume - buckets[-1].sell_volume
    net_5m = sum_last(5, "buy_volume") - sum_last(5, "sell_volume")
    net_15m = sum_last(15, "buy_volume") - sum_last(15, "sell_volume")
    net_60m = sum_last(60, "buy_volume") - sum_last(60, "sell_volume")

    total_60 = sum(b.volume for b in buckets)
    avg_60 = total_60 / max(1, len(buckets))
    vol_1m_vs_60m_avg = (vol_1m / avg_60 * 100) if avg_60 > 0 else 0.0

    return {
        "vol_1m": vol_1m,
        "vol_5m": vol_5m,
        "vol_15m": vol_15m,
        "vol_60m": vol_60m,
        "net_1m": net_1m,
        "net_5m": net_5m,
        "net_15m": net_15m,
        "net_60m": net_60m,
        "vol_1m_vs_60m_avg": vol_1m_vs_60m_avg,
    }


def _update_pings(state: SymbolState, net_1m: float, quote_volume_24h: float) -> int:
    """
    Ping = net_1m > 0.3% من 24h volume (إشارة قوية للحركة في اتجاه واحد).
    يخزن timestamps في آخر 24 ساعة فقط.
    """
    now = int(time.time())
    # تنظيف القديم
    day_ago = now - 86400
    while state.ping_timestamps and state.ping_timestamps[0] < day_ago:
        state.ping_timestamps.popleft()

    if quote_volume_24h > 0 and net_1m > 0.003 * quote_volume_24h:
        state.ping_timestamps.append(now)

    return len(state.ping_timestamps)


def classify_symbol(
    symbol: str,
    last_price: float,
    price_change_percent: float,
    quote_volume_24h: float,
    best_bid: float,
    best_ask: float,
    vol_1m: float,
    vol_5m: float,
    vol_15m: float,
    vol_60m: float,
    net_1m: float,
    net_5m: float,
    net_15m: float,
    net_60m: float,
    vol_1m_vs_60m_avg: float,
    pings_24h: int,
) -> SymbolStats:
    # spread %
    spread = 0.0
    if last_price > 0 and best_bid > 0 and best_ask > 0:
        mid = (best_bid + best_ask) / 2
        if mid > 0:
            spread = (best_ask - best_bid) / mid * 100

    # اتجاه السعر 24h
    if price_change_percent > 0:
        side: SideType = "long"
    elif price_change_percent < 0:
        side = "short"
    else:
        side = "neutral"

    abs_change = abs(price_change_percent)

    # نحاول نعمل score من كذا عامل
    score = 0

    # فوليوم 24h محترم
    if quote_volume_24h >= 3_000_000:
        score += 1

    # boost لو الدقيقة الحالية أعلى من متوسط 60m
    if vol_1m_vs_60m_avg > 200:  # 200% من المتوسط
        score += 1
    if vol_1m_vs_60m_avg > 400:
        score += 1

    # حركة سعر محترمة
    if abs_change > 3:
        score += 1
    if abs_change > 7:
        score += 1

    # Ping قوي
    if pings_24h >= 1:
        score += 1
    if pings_24h >= 3:
        score += 1

    if score <= 1:
        strength: SignalStrength = "weak"
    elif score <= 3:
        strength = "good"
    elif score <= 5:
        strength = "strong"
    else:
        strength = "very strong"

    return SymbolStats(
        symbol=symbol,
        last_price=last_price,
        price_change_percent=price_change_percent,
        quote_volume_24h=quote_volume_24h,
        best_bid=best_bid,
        best_ask=best_ask,
        spread_percent=spread,
        strength=strength,
        side=side,
        vol_1m=vol_1m,
        vol_5m=vol_5m,
        vol_15m=vol_15m,
        vol_60m=vol_60m,
        net_1m=net_1m,
        net_5m=net_5m,
        net_15m=net_15m,
        net_60m=net_60m,
        vol_1m_vs_60m_avg=vol_1m_vs_60m_avg,
        pings_24h=pings_24h,
    )


# =========================
#  Binance WebSocket
# =========================

async def binance_ws_listener():
    """
    WebSocket على !ticker@arr
    - نستخدم فرق 24h quote volume كـ delta لكل رسالة
    - نكوم الدلتا دي في buckets بالدقيقة
    - نطلع منها volume windows + net volume + pings + strength
    """

    global symbol_data, _symbol_state

    while True:
        try:
            async with websockets.connect(BINANCE_WS_URL, ping_interval=20) as ws:
                print("✅ Connected to Binance WebSocket")

                async for msg in ws:
                    try:
                        data = json.loads(msg)
                    except json.JSONDecodeError:
                        continue

                    if not isinstance(data, list):
                        continue

                    now = time.time()

                    for t in data:
                        try:
                            symbol = t.get("s")
                            if not symbol or not symbol.endswith("USDT"):
                                continue

                            last_price = float(t.get("c", 0.0))
                            price_change_percent = float(t.get("P", 0.0))
                            quote_volume_24h = float(t.get("q", 0.0))
                            best_bid = float(t.get("b", 0.0))
                            best_ask = float(t.get("a", 0.0))

                            state = _symbol_state.get(symbol)
                            if state is None:
                                state = SymbolState()
                                _symbol_state[symbol] = state

                            # delta 24h volume
                            prev_q = state.last_quote_24h
                            state.last_quote_24h = quote_volume_24h
                            delta_quote = max(0.0, quote_volume_24h - prev_q)

                            # اتجاه الحركة الحالية (بناءً على تغير السعر)
                            direction = 0
                            if state.last_price > 0:
                                if last_price > state.last_price:
                                    direction = 1
                                elif last_price < state.last_price:
                                    direction = -1
                            state.last_price = last_price

                            # تحديث buckets
                            if delta_quote > 0:
                                _update_minute_buckets(state, delta_quote, direction)

                            # حساب volume windows
                            vol_info = _compute_volume_windows(state)

                            # تحديث pings بناءً على net_1m
                            pings_24h = _update_pings(
                                state,
                                vol_info["net_1m"],
                                quote_volume_24h,
                            )

                            stats = classify_symbol(
                                symbol=symbol,
                                last_price=last_price,
                                price_change_percent=price_change_percent,
                                quote_volume_24h=quote_volume_24h,
                                best_bid=best_bid,
                                best_ask=best_ask,
                                vol_1m=vol_info["vol_1m"],
                                vol_5m=vol_info["vol_5m"],
                                vol_15m=vol_info["vol_15m"],
                                vol_60m=vol_info["vol_60m"],
                                net_1m=vol_info["net_1m"],
                                net_5m=vol_info["net_5m"],
                                net_15m=vol_info["net_15m"],
                                net_60m=vol_info["net_60m"],
                                vol_1m_vs_60m_avg=vol_info["vol_1m_vs_60m_avg"],
                                pings_24h=pings_24h,
                            )

                            symbol_data[symbol] = stats

                        except Exception:
                            continue

        except Exception as e:
            print("⚠️ WebSocket error, reconnecting in 5s:", e)
            await asyncio.sleep(5)


# =========================
#  FastAPI
# =========================

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(binance_ws_listener())


@app.get("/health")
async def health():
    return {"status": "ok", "symbols_cached": len(symbol_data)}


@app.get("/symbols", response_model=List[SymbolStats])
async def get_symbols(limit: int = 100):
    ordered = sorted(
        symbol_data.values(),
        key=lambda s: s.quote_volume_24h,
        reverse=True,
    )
    return ordered[:limit]


@app.get("/signals", response_model=List[SymbolStats])
async def get_signals(limit: int = 100):
    """
    مؤقتًا: رجّع كل العملات بدون أي فلترة
    علشان نتأكد إن الـ frontend موصول صح.
    """
    ordered = sorted(
        symbol_data.values(),
        key=lambda s: s.quote_volume_24h,
        reverse=True,
    )
    return ordered[:limit]


    """
    فلترة العملات كـ "إشارات":
    - حد أدنى لفوليوم 24h
    - حد أدنى لنسبة فوليوم الدقيقة مقابل متوسط 60 دقيقة
    - حد أدنى لعدد الـ pings خلال 24 ساعة
    - اتجاه (اختياري)
    - حد أدنى للـ strength
    """
    strength_order = {"weak": 0, "good": 1, "strong": 2, "very strong": 3}

    result: List[SymbolStats] = []

    for s in symbol_data.values():
        if s.quote_volume_24h < min_volume_24h:
            continue
        if s.vol_1m_vs_60m_avg < min_vol_1m_vs_avg:
            continue
        if s.pings_24h < min_ping_24h:
            continue
        if side is not None and s.side != side:
            continue
        if strength_order[s.strength] < strength_order[min_strength]:
            continue

        result.append(s)

    result.sort(key=lambda s: s.quote_volume_24h, reverse=True)
    return result[:limit]
