"""FinMind 備援資料源（免費版：逐檔查詢；可在設定頁填 token 提高額度）。"""
from .http import get_json

API = "https://api.finmindtrade.com/api/v4/data"

# FinMind 持股分級名稱 → TDCC 級距編號
_LEVEL_MAP = {
    "1-999": 1, "1,000-5,000": 2, "5,001-10,000": 3, "10,001-15,000": 4,
    "15,001-20,000": 5, "20,001-30,000": 6, "30,001-40,000": 7,
    "40,001-50,000": 8, "50,001-100,000": 9, "100,001-200,000": 10,
    "200,001-400,000": 11, "400,001-600,000": 12, "600,001-800,000": 13,
    "800,001-1,000,000": 14, "more than 1,000,001": 15, "total": 17,
}


class FinMindAuthError(RuntimeError):
    """token 缺少或等級不足。"""


def _query(dataset: str, data_id: str, start_date: str, token: str | None):
    params = {"dataset": dataset, "data_id": data_id, "start_date": start_date}
    if token:
        params["token"] = token
    try:
        j = get_json(API, params=params, min_interval=0.35, timeout=60, retries=0)
    except Exception as e:
        resp = getattr(e, "response", None)
        if resp is not None:
            try:
                msg = resp.json().get("msg", "")
            except ValueError:
                msg = resp.text[:200]
            if "level" in msg or resp.status_code in (400, 402):
                raise FinMindAuthError(
                    f"FinMind 拒絕（{msg}）— 此資料集需要免費註冊的 API token，"
                    "請至 finmindtrade.com 註冊後在設定頁填入") from e
            raise RuntimeError(f"FinMind: {msg}") from e
        raise
    if j.get("status") != 200:
        raise RuntimeError(f"FinMind: {j.get('msg')}")
    return j.get("data", [])


def fetch_stock_price(code: str, start_date: str, token: str | None = None) -> list[tuple]:
    data = _query("TaiwanStockPrice", code, start_date, token)
    return [(code, d["date"], d.get("open"), d.get("max"), d.get("min"),
             d.get("close"), int(d.get("Trading_Volume") or 0)) for d in data]


def fetch_stock_insti(code: str, start_date: str, token: str | None = None) -> list[tuple]:
    data = _query("TaiwanStockInstitutionalInvestorsBuySell", code, start_date, token)
    by_date: dict[str, dict] = {}
    for d in data:
        rec = by_date.setdefault(d["date"], {"f": 0, "t": 0, "d": 0})
        net = int(d.get("buy") or 0) - int(d.get("sell") or 0)
        name = d.get("name", "")
        if name.startswith("Foreign"):
            rec["f"] += net
        elif name == "Investment_Trust":
            rec["t"] += net
        elif name.startswith("Dealer"):
            rec["d"] += net
    return [(code, dt, r["f"], r["t"], r["d"], r["f"] + r["t"] + r["d"])
            for dt, r in sorted(by_date.items())]


def fetch_stock_tdcc(code: str, start_date: str, token: str | None = None) -> list[tuple]:
    """集保股權分散歷史（逐檔）。回傳 (code,date,level,holders,shares,pct)。"""
    data = _query("TaiwanStockHoldingSharesPer", code, start_date, token)
    rows = []
    for d in data:
        lv = _LEVEL_MAP.get(d.get("HoldingSharesLevel", ""))
        if lv is None:
            continue
        rows.append((code, d["date"], lv, int(d.get("people") or 0),
                     int(d.get("unit") or 0), float(d.get("percent") or 0)))
    return rows
