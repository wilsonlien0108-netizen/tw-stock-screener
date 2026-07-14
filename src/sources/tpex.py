"""櫃買中心（上櫃）資料源：公司名單、每日收盤行情、三大法人買賣超。"""
from .http import get_json, num

OPENAPI = "https://www.tpex.org.tw/openapi/v1"
WWW = "https://www.tpex.org.tw/www/zh-tw"
THROTTLE = 2.0


def _roc(date_ymd: str) -> str:
    """YYYYMMDD -> ROC 格式 115/07/07"""
    return f"{int(date_ymd[:4]) - 1911}/{date_ymd[4:6]}/{date_ymd[6:]}"


def _iso(date_ymd: str) -> str:
    return f"{date_ymd[:4]}-{date_ymd[4:6]}-{date_ymd[6:]}"


def fetch_company_list() -> list[tuple]:
    data = get_json(f"{OPENAPI}/mopsfin_t187ap03_O", min_interval=1.0)
    rows = []
    for r in data:
        code = (r.get("SecuritiesCompanyCode") or "").strip()
        if len(code) == 4 and code.isdigit():
            rows.append((code, (r.get("CompanyAbbreviation") or "").strip(), "TPEX",
                         (r.get("SecuritiesIndustryCode") or "").strip().zfill(2)))
    return rows


def fetch_prices_by_date(date_ymd: str) -> list[tuple] | None:
    """上櫃每日收盤行情。休市日回傳 None。"""
    j = get_json(f"{WWW}/afterTrading/otc",
                 params={"date": _roc(date_ymd), "type": "EW", "response": "json"},
                 min_interval=THROTTLE)
    tables = j.get("tables") or []
    if not tables or not tables[0].get("data"):
        return None
    resp_date = (tables[0].get("date") or "").strip()
    if resp_date and resp_date != _roc(date_ymd):  # 回應日期不符 → 視為無資料
        return None
    date_iso = _iso(date_ymd)
    rows = []
    for d in tables[0]["data"]:
        code = d[0].strip()
        close = num(d[2])
        if close is None:
            continue
        rows.append((code, date_iso, num(d[4]), num(d[5]), num(d[6]), close,
                     int(num(d[7]) or 0)))
    return rows or None


def fetch_insti_by_date(date_ymd: str) -> list[tuple] | None:
    """上櫃三大法人買賣超（個股）。欄位分組：
    外資及陸資(不含自營) / 外資自營 / 外資合計 / 投信 / 自營(自行) / 自營(避險) / 自營合計 / 三大法人合計"""
    j = get_json(f"{WWW}/insti/dailyTrade",
                 params={"type": "Daily", "sect": "EW", "response": "json",
                         "date": _roc(date_ymd)},
                 min_interval=THROTTLE)
    tables = j.get("tables") or []
    if not tables or not tables[0].get("data"):
        return None
    resp_date = (tables[0].get("date") or "").strip()
    if resp_date and resp_date != _roc(date_ymd):
        return None
    date_iso = _iso(date_ymd)
    rows = []
    for d in tables[0]["data"]:
        code = d[0].strip()
        if len(d) >= 24:
            foreign = num(d[10]) or 0     # 外資及陸資合計買賣超
            trust = num(d[13]) or 0
            dealer = num(d[22]) or 0      # 自營商合計買賣超
            total = num(d[23]) or 0
        else:  # 舊版欄位（外資/投信/自營/合計）
            foreign = num(d[4]) or 0
            trust = num(d[7]) or 0
            dealer = num(d[10]) or 0
            total = num(d[11]) or 0
        rows.append((code, date_iso, int(foreign), int(trust), int(dealer), int(total)))
    return rows or None


def fetch_revenue() -> list[tuple]:
    """上櫃公司月營收彙總（欄位同上市版）。"""
    data = get_json(f"{OPENAPI}/mopsfin_t187ap05_O", min_interval=1.0)
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
    """上櫃個股本益比/殖利率/股價淨值比。"""
    data = get_json(f"{OPENAPI}/tpex_mainboard_peratio_analysis", min_interval=1.0)
    rows = []
    for r in data:
        code = (r.get("SecuritiesCompanyCode") or "").strip()
        roc = (r.get("Date") or "").strip()
        if len(code) != 4 or not code.isdigit() or len(roc) != 7:
            continue
        date_iso = f"{int(roc[:3]) + 1911}-{roc[3:5]}-{roc[5:]}"
        rows.append((code, date_iso, num(r.get("PriceEarningRatio")),
                     num(r.get("YieldRatio")), num(r.get("PriceBookRatio"))))
    return rows


def fetch_margin_by_date(date_ymd: str) -> list[tuple] | None:
    """上櫃融資融券餘額（張）。休市日回傳 None。"""
    j = get_json(f"{WWW}/margin/balance",
                 params={"date": _roc(date_ymd), "response": "json"},
                 min_interval=THROTTLE)
    tables = j.get("tables") or []
    if not tables or not tables[0].get("data"):
        return None
    resp_date = (tables[0].get("date") or "").strip()
    if resp_date and resp_date != _roc(date_ymd):
        return None
    date_iso = _iso(date_ymd)
    rows = []
    for d in tables[0]["data"]:
        code = d[0].strip()
        # 欄位: 2前資餘額 6資餘額 10前券餘額 14券餘額
        fin_prev, fin_bal = num(d[2]) or 0, num(d[6]) or 0
        sh_prev, sh_bal = num(d[10]) or 0, num(d[14]) or 0
        rows.append((code, date_iso, int(fin_bal), int(fin_bal - fin_prev),
                     int(sh_bal), int(sh_bal - sh_prev)))
    return rows or None


def fetch_prices_latest() -> tuple[str, list[tuple]] | None:
    """OpenAPI 上櫃最近交易日行情（備援用）。"""
    data = get_json(f"{OPENAPI}/tpex_mainboard_quotes", min_interval=1.0)
    if not data:
        return None
    roc = data[0].get("Date", "")
    if len(roc) != 7:
        return None
    date_iso = f"{int(roc[:3]) + 1911}-{roc[3:5]}-{roc[5:]}"
    rows = []
    for r in data:
        code = (r.get("SecuritiesCompanyCode") or "").strip()
        close = num(r.get("Close"))
        if close is None:
            continue
        rows.append((code, date_iso, num(r.get("Open")), num(r.get("High")),
                     num(r.get("Low")), close, int(num(r.get("TradingShares")) or 0)))
    return (date_iso, rows) if rows else None
