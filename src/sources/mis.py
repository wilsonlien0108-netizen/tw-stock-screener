"""證交所 MIS 盤中即時報價（非官方介面）：分批查詢、輕度節流。"""
from .http import get_json, num

URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
BATCH = 100
THROTTLE = 0.4


def fetch_quotes(codes_markets: list[tuple[str, str]]) -> dict[str, dict]:
    """codes_markets: [(code, 'TWSE'|'TPEX'), ...]
    回傳 {code: {price, prev_close, volume, time, name}}；price 可能為 None（尚無成交）。"""
    result: dict[str, dict] = {}
    items = [f"{'tse' if m == 'TWSE' else 'otc'}_{c}.tw" for c, m in codes_markets]
    for i in range(0, len(items), BATCH):
        chunk = "|".join(items[i:i + BATCH])
        try:
            j = get_json(URL, params={"ex_ch": chunk, "json": "1", "delay": "0"},
                         min_interval=THROTTLE, retries=1)
        except Exception:
            continue
        for q in j.get("msgArray", []):
            code = (q.get("c") or "").strip()
            if not code:
                continue
            price = num(q.get("z"))
            if price is None:
                # 尚無成交：用最佳買/賣價中價估，最後退回昨收
                bid = num((q.get("b") or "").split("_")[0])
                ask = num((q.get("a") or "").split("_")[0])
                if bid and ask:
                    price = round((bid + ask) / 2, 2)
                elif bid or ask:
                    price = bid or ask
            result[code] = {
                "price": price,
                "prev_close": num(q.get("y")),
                "volume": num(q.get("v")),
                "time": q.get("t"),
                "name": q.get("n"),
            }
    return result
