"""證交所（上市）資料源：公司名單、每日收盤行情、三大法人買賣超。"""
import re

from .http import get_json, num

OPENAPI = "https://openapi.twse.com.tw/v1"
RWD = "https://www.twse.com.tw/rwd/zh"
THROTTLE = 3.0  # 證交所 rwd 介面有速率限制，逐日抓取需放慢


def fetch_company_list() -> list[tuple]:
    """上市公司基本資料（僅普通股公司，不含 ETF/權證）。回傳 (code,name,market,industry)。"""
    data = get_json(f"{OPENAPI}/opendata/t187ap03_L", min_interval=1.0)
    rows = []
    for r in data:
        code = (r.get("公司代號") or "").strip()
        if len(code) == 4 and code.isdigit():
            rows.append((code, (r.get("公司簡稱") or "").strip(), "TWSE",
                         (r.get("產業別") or "").strip()))
    return rows


def fetch_prices_by_date(date_ymd: str) -> list[tuple] | None:
    """MI_INDEX 每日收盤行情（全部）。date_ymd 格式 YYYYMMDD。休市日回傳 None。
    回傳 (code, date, open, high, low, close, volume)。"""
    j = get_json(f"{RWD}/afterTrading/MI_INDEX",
                 params={"date": date_ymd, "type": "ALLBUT0999", "response": "json"},
                 min_interval=THROTTLE)
    tables = j.get("tables") or []
    target = None
    for t in tables:
        if "每日收盤行情" in (t.get("title") or ""):
            target = t
    if target is None:
        return None
    # 防護：休市日請求時伺服器可能回覆「最近交易日」的內容——
    # 驗證回應標題內的日期與請求日期一致，不一致視為當日無資料
    m = re.search(r"(\d{3})年(\d{2})月(\d{2})日", target.get("title") or "")
    if m:
        resp_ymd = f"{int(m.group(1)) + 1911}{m.group(2)}{m.group(3)}"
        if resp_ymd != date_ymd:
            return None
    date_iso = f"{date_ymd[:4]}-{date_ymd[4:6]}-{date_ymd[6:]}"
    rows = []
    for d in target.get("data", []):
        code = d[0].strip()
        close = num(d[8])
        if close is None:
            continue
        rows.append((code, date_iso, num(d[5]), num(d[6]), num(d[7]), close,
                     int(num(d[2]) or 0)))
    return rows or None


def fetch_insti_by_date(date_ymd: str) -> list[tuple] | None:
    """T86 三大法人買賣超（個股）。休市日回傳 None。
    回傳 (code, date, foreign_net, trust_net, dealer_net, total_net)，單位：股。"""
    j = get_json(f"{RWD}/fund/T86",
                 params={"date": date_ymd, "selectType": "ALLBUT0999", "response": "json"},
                 min_interval=THROTTLE)
    if j.get("stat") != "OK" or not j.get("data"):
        return None
    if (j.get("date") or date_ymd) != date_ymd:  # 回應日期與請求不符 → 視為無資料
        return None
    date_iso = f"{date_ymd[:4]}-{date_ymd[4:6]}-{date_ymd[6:]}"
    rows = []
    for d in j["data"]:
        code = d[0].strip()
        foreign = (num(d[4]) or 0) + (num(d[7]) or 0)   # 外陸資 + 外資自營商
        trust = num(d[10]) or 0
        dealer = num(d[11]) or 0                         # 自營商合計
        total = num(d[18]) or 0
        rows.append((code, date_iso, int(foreign), int(trust), int(dealer), int(total)))
    return rows or None


def fetch_revenue() -> list[tuple]:
    """上市公司月營收彙總（含 MoM/YoY%）。回傳 (code, 'YYYY-MM', revenue千元, mom, yoy, cum_yoy)。"""
    data = get_json(f"{OPENAPI}/opendata/t187ap05_L", min_interval=1.0)
    rows = []
    for r in data:
        code = (r.get("公司代號") or "").strip()
        ym = (r.get("資料年月") or "").strip()
        if len(code) != 4 or not code.isdigit() or len(ym) != 5:
            continue
        month = f"{int(ym[:3]) + 1911}-{ym[3:]}"
        rows.append((code, month, int(num(r.get("營業收入-當月營收")) or 0),
                     num(r.get("營業收入-上月比較增減(%)")),
                     num(r.get("營業收入-去年同月增減(%)")),
                     num(r.get("累計營業收入-前期比較增減(%)"))))
    return rows


def fetch_valuation() -> list[tuple]:
    """上市個股每日本益比/殖利率/股價淨值比。回傳 (code, date, pe, yield, pb)。"""
    data = get_json(f"{OPENAPI}/exchangeReport/BWIBBU_ALL", min_interval=1.0)
    rows = []
    for r in data:
        code = (r.get("Code") or "").strip()
        roc = (r.get("Date") or "").strip()
        if len(code) != 4 or not code.isdigit() or len(roc) != 7:
            continue
        date_iso = f"{int(roc[:3]) + 1911}-{roc[3:5]}-{roc[5:]}"
        rows.append((code, date_iso, num(r.get("PEratio")),
                     num(r.get("DividendYield")), num(r.get("PBratio"))))
    return rows


def fetch_margin_by_date(date_ymd: str) -> list[tuple] | None:
    """MI_MARGN 融資融券彙總（個股，單位：張）。休市日回傳 None。
    回傳 (code, date, fin_balance, fin_chg, short_balance, short_chg)。"""
    j = get_json(f"{RWD}/marginTrading/MI_MARGN",
                 params={"date": date_ymd, "selectType": "ALL", "response": "json"},
                 min_interval=THROTTLE)
    tables = j.get("tables") or []
    target = None
    for t in tables:
        if "融資融券彙總" in (t.get("title") or ""):
            target = t
    if target is None or not target.get("data"):
        return None
    m = re.search(r"(\d{3})年(\d{2})月(\d{2})日", target.get("title") or "")
    if m:
        resp_ymd = f"{int(m.group(1)) + 1911}{m.group(2)}{m.group(3)}"
        if resp_ymd != date_ymd:
            return None
    date_iso = f"{date_ymd[:4]}-{date_ymd[4:6]}-{date_ymd[6:]}"
    rows = []
    for d in target["data"]:
        code = d[0].strip()
        fin_prev, fin_bal = num(d[5]) or 0, num(d[6]) or 0
        sh_prev, sh_bal = num(d[11]) or 0, num(d[12]) or 0
        rows.append((code, date_iso, int(fin_bal), int(fin_bal - fin_prev),
                     int(sh_bal), int(sh_bal - sh_prev)))
    return rows or None


def fetch_taiex_month(date_ymd: str) -> list[tuple]:
    """FMTQIK 市場成交資訊（整月）。回傳 [(date_iso, taiex), ...]。"""
    j = get_json(f"{RWD}/afterTrading/FMTQIK",
                 params={"date": date_ymd, "response": "json"}, min_interval=THROTTLE)
    if j.get("stat") != "OK":
        return []
    rows = []
    for d in j.get("data", []):
        y, m, dd = d[0].split("/")
        idx = num(d[4])
        if idx:
            rows.append((f"{int(y) + 1911}-{m}-{dd}", idx))
    return rows


def fetch_prices_latest() -> tuple[str, list[tuple]] | None:
    """OpenAPI STOCK_DAY_ALL：最近一個交易日全部個股（備援用）。"""
    data = get_json(f"{OPENAPI}/exchangeReport/STOCK_DAY_ALL", min_interval=1.0)
    if not data:
        return None
    roc = data[0].get("Date", "")
    if len(roc) != 7:
        return None
    date_iso = f"{int(roc[:3]) + 1911}-{roc[3:5]}-{roc[5:]}"
    rows = []
    for r in data:
        code = (r.get("Code") or "").strip()
        close = num(r.get("ClosingPrice"))
        if close is None:
            continue
        rows.append((code, date_iso, num(r.get("OpeningPrice")), num(r.get("HighestPrice")),
                     num(r.get("LowestPrice")), close, int(num(r.get("TradeVolume")) or 0)))
    return (date_iso, rows) if rows else None
