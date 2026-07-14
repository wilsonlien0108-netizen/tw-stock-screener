"""集保結算所開放資料：集保戶股權分散表（每週五更新，含 15 個持股級距）。

級距對照（張 = 1000 股）：
 1: 1-999股        2: 1-5張      3: 5-10張    4: 10-15張   5: 15-20張
 6: 20-30張        7: 30-40張    8: 40-50張   9: 50-100張 10: 100-200張
11: 200-400張     12: 400-600張 13: 600-800張 14: 800-1000張 15: 1000張以上
16: 差異數調整    17: 合計
"""
import csv
import io

from .http import get

URL = "https://opendata.tdcc.com.tw/getOD.ashx?id=1-5"

# 大戶門檻（張）→ 最低級距
THRESHOLD_LEVEL = {100: 10, 200: 11, 400: 12, 600: 13, 800: 14, 1000: 15}
RETAIL_MAX_LEVEL = 3  # 散戶 = 10 張以下（級距 1-3）


def fetch_latest() -> tuple[str, list[tuple]] | None:
    """回傳 (date_iso, [(code, date, level, holders, shares, pct), ...])，只含最新一週。"""
    r = get(URL, min_interval=1.0, timeout=120)
    text = r.content.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if not header:
        return None
    rows = []
    date_iso = None
    for row in reader:
        if len(row) < 6:
            continue
        raw_date, code, level, holders, shares, pct = row[:6]
        if not (raw_date and code and level):
            continue
        code = code.strip()
        if len(code) != 4 or not code.isdigit():
            continue  # 只留 4 碼普通股
        try:
            lv = int(level)
            d = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}"
            rows.append((code, d, lv, int(holders or 0), int(shares or 0),
                         float(pct or 0)))
            date_iso = d
        except ValueError:
            continue
    return (date_iso, rows) if rows else None
