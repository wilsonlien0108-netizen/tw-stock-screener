"""一鍵資料健檢：完整性掃描 + 獨立官方端點抽樣比對 + 指標迴圈對照。

每項檢查回傳 {"項目", "狀態"("✅"/"❌"/"⚠️"), "說明"}。
"""
import random
import re
import statistics
from datetime import date, timedelta

from . import db, indicators
from .sources.http import get_json, num


def _check_row_counts(con) -> list[dict]:
    out = []
    for table, label, thr in (("prices", "股價", 0.95), ("insti", "法人", 0.90),
                              ("margin", "融資融券", 0.90)):
        rows = con.execute(f"SELECT date, COUNT(*) FROM {table} GROUP BY date").fetchall()
        if len(rows) < 5:
            out.append({"項目": f"{label}每日筆數", "狀態": "⚠️",
                        "說明": "資料太少，略過檢查"})
            continue
        med = statistics.median([n for _, n in rows])
        bad = [(d, n) for d, n in rows if n < med * thr]
        out.append({"項目": f"{label}每日筆數完整性", "狀態": "✅" if not bad else "❌",
                    "說明": f"{len(rows)} 日，中位數 {int(med)} 筆"
                    + ("" if not bad else f"；異常 {bad[:5]}（每日更新會自動修復）")})
    return out


def _check_holiday_dup(con) -> dict:
    dates = db.price_dates(con)[-40:]
    dups = []
    for a, b in zip(dates, dates[1:]):
        n = con.execute(
            "SELECT COUNT(*) FROM prices x JOIN prices y ON x.code=y.code"
            " AND x.date=? AND y.date=? AND x.close=y.close AND x.volume=y.volume"
            " AND x.open=y.open", (b, a)).fetchone()[0]
        tot = con.execute("SELECT COUNT(*) FROM prices WHERE date=?", (b,)).fetchone()[0]
        if tot > 100 and n / tot > 0.99:
            dups.append(b)
    return {"項目": "休市日重複資料掃描（近40日）", "狀態": "✅" if not dups else "❌",
            "說明": "無整日重複" if not dups else f"疑似重複日：{dups}"}


def _check_price_external(con) -> dict:
    """抽樣：台積電近月收盤 vs 證交所 STOCK_DAY 獨立端點。"""
    try:
        ymd = date.today().strftime("%Y%m01")
        j = get_json("https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY",
                     params={"date": ymd, "stockNo": "2330", "response": "json"},
                     min_interval=3.0)
        bad = checked = 0
        for row in j.get("data", []):
            y, m, d_ = row[0].split("/")
            iso = f"{int(y) + 1911}-{m}-{d_}"
            official = num(row[6])
            got = con.execute("SELECT close FROM prices WHERE code='2330' AND date=?",
                              (iso,)).fetchone()
            if got and official:
                checked += 1
                if abs(got[0] - official) > 0.01:
                    bad += 1
        return {"項目": "股價外部比對（2330 vs STOCK_DAY）",
                "狀態": "✅" if bad == 0 and checked else "❌",
                "說明": f"比對 {checked} 日，不符 {bad} 筆"}
    except Exception as e:
        return {"項目": "股價外部比對", "狀態": "⚠️", "說明": f"端點暫時無法連線：{e}"}


def _check_insti_external(con) -> dict:
    """抽樣：台積電最新外資買賣超 vs TWT38U 獨立報表。"""
    try:
        iso = db.insti_dates(con)[-1]
        j = get_json("https://www.twse.com.tw/rwd/zh/fund/TWT38U",
                     params={"date": iso.replace("-", ""), "response": "json"},
                     min_interval=3.0)
        official = None
        for row in j.get("data", []):
            if row[1].strip() == "2330":
                official = (num(row[5]) or 0) + (num(row[8]) or 0)
                break
        got = con.execute("SELECT foreign_net FROM insti WHERE code='2330' AND date=?",
                          (iso,)).fetchone()
        ok = official is not None and got and abs(got[0] - official) < 1
        return {"項目": "外資買賣超外部比對（2330 vs TWT38U）",
                "狀態": "✅" if ok else "❌",
                "說明": f"{iso}: DB={got[0]:+,} 官方={official:+,.0f}" if got and official is not None
                else "無法取得比對值"}
    except Exception as e:
        return {"項目": "外資買賣超外部比對", "狀態": "⚠️", "說明": f"端點暫時無法連線：{e}"}


def _check_indicators(con) -> dict:
    """KD/RSI：教科書迴圈 vs 向量化（2330 + 隨機 2 檔）。"""
    prices = db.load_prices(con, 300)
    if prices.empty:
        return {"項目": "指標演算對照", "狀態": "⚠️", "說明": "無資料"}
    pv = indicators.make_pivots(prices)
    ind = indicators.compute(pv)
    candidates = [c for c in pv["close"].columns
                  if pv["close"][c].notna().sum() > 150]
    codes = ["2330"] + random.sample(candidates, 2)
    bad = []
    for code in codes:
        h = pv["high"][code].dropna()
        l = pv["low"][code].reindex(h.index)
        c = pv["close"][code].reindex(h.index)
        K = D = 50.0
        for i in range(len(c)):
            if i >= 8:
                lo, hi = l.iloc[i - 8:i + 1].min(), h.iloc[i - 8:i + 1].max()
                rsv = (c.iloc[i] - lo) / (hi - lo) * 100 if hi > lo else 50.0
            else:
                rsv = 50.0
            K = K * 2 / 3 + rsv / 3
            D = D * 2 / 3 + K / 3
        vk = ind["k"][code].loc[h.index[-1]]
        vd = ind["d"][code].loc[h.index[-1]]
        if abs(K - vk) > 0.5 or abs(D - vd) > 0.5:
            bad.append(f"{code}(K {K:.1f}vs{vk:.1f})")
    return {"項目": "KD 演算對照（迴圈 vs 向量化，3 檔）",
            "狀態": "✅" if not bad else "❌",
            "說明": "一致" if not bad else "、".join(bad)}


def _check_tdcc(con) -> dict:
    rows = con.execute("""
      SELECT COUNT(*) FROM (
        SELECT code, MAX(CASE WHEN level=17 THEN pct END) t FROM tdcc
        WHERE date=(SELECT MAX(date) FROM tdcc) GROUP BY code
        HAVING t IS NOT NULL AND ABS(t-100.0) > 0.5)""").fetchone()[0]
    return {"項目": "集保合計檢查", "狀態": "✅" if rows == 0 else "❌",
            "說明": "全部股票合計=100%" if rows == 0 else f"{rows} 檔合計異常"}


def _check_freshness(con) -> dict:
    last = db.price_dates(con)
    if not last:
        return {"項目": "資料時效", "狀態": "❌", "說明": "沒有任何股價資料"}
    gap = (date.today() - date.fromisoformat(last[-1])).days
    status = "✅" if gap <= 4 else "⚠️"
    return {"項目": "資料時效", "狀態": status,
            "說明": f"最新股價：{last[-1]}（{gap} 天前）"
            + ("" if gap <= 4 else "，建議執行每日更新")}


def run_all(con) -> list[dict]:
    results = []
    results.append(_check_freshness(con))
    results += _check_row_counts(con)
    results.append(_check_holiday_dup(con))
    results.append(_check_tdcc(con))
    results.append(_check_indicators(con))
    results.append(_check_price_external(con))
    results.append(_check_insti_external(con))
    return results
