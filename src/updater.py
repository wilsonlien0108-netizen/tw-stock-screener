"""資料更新協調器：股票清單、股價/法人逐日回補、集保週資料、盤中即時。
主來源失敗時自動切換備援（OpenAPI 最新日快照 / FinMind），並記錄各來源健康狀態。"""
from datetime import date, datetime, timedelta

from . import db
from .sources import finmind, mis, mops, tdcc, tpex, twse


def _log(progress, msg: str, frac: float | None = None):
    if progress:
        progress(msg, frac)


# ---------- 股票清單 ----------

def update_universe(con, progress=None) -> int:
    rows = []
    for name, mod in (("TWSE", twse), ("TPEX", tpex)):
        try:
            part = mod.fetch_company_list()
            rows += part
            db.record_health(con, "股票清單", name, True, f"{len(part)} 檔")
        except Exception as e:
            db.record_health(con, "股票清單", name, False, str(e))
    if rows:
        db.upsert_many(con, "stocks", ["code", "name", "market", "industry_code",
                                       "industry_name"],
                       [(c, n, m, ic, db.INDUSTRY.get(ic, ic)) for c, n, m, ic in rows])
        db.set_meta(con, "universe_updated", date.today().isoformat())
    _log(progress, f"股票清單更新完成：{len(rows)} 檔")
    return len(rows)


def _universe_codes(con) -> set[str]:
    return {r[0] for r in con.execute("SELECT code FROM stocks").fetchall()}


# ---------- 股價（逐日回補） ----------

def backfill_prices(con, n_trading_days: int = 60, progress=None) -> int:
    """從今天往回抓，直到湊滿 n_trading_days 個交易日（已有的日期會跳過）。"""
    codes = _universe_codes(con)
    have = set(db.price_dates(con))
    added_days = 0
    counted = 0
    d = date.today()
    scanned = 0
    while counted < n_trading_days and scanned < n_trading_days * 2 + 30:
        scanned += 1
        if d.weekday() >= 5:  # 週末
            d -= timedelta(days=1)
            continue
        iso = d.isoformat()
        ymd = d.strftime("%Y%m%d")
        if iso in have:
            counted += 1
            d -= timedelta(days=1)
            continue
        day_rows = []
        ok_any = False  # 至少一個來源有回應（含休市日的空回應）
        for name, mod in (("TWSE", twse), ("TPEX", tpex)):
            try:
                rows = mod.fetch_prices_by_date(ymd)
                ok_any = True
                if rows:
                    day_rows += [r for r in rows if r[0] in codes]
                db.record_health(con, "每日股價", name, True,
                                 f"{iso} {'有' if rows else '休市/無'}資料")
            except Exception as e:
                db.record_health(con, "每日股價", name, False, f"{iso}: {e}")
        if day_rows:
            db.upsert_many(con, "prices",
                           ["code", "date", "open", "high", "low", "close", "volume"],
                           day_rows)
            counted += 1
            added_days += 1
            _log(progress, f"已回補 {iso}（{len(day_rows)} 檔）",
                 min(counted / n_trading_days, 1.0))
        elif ok_any is False:
            # 兩邊都失敗（非休市）→ 試備援：OpenAPI 只有最近一日
            _fallback_latest_prices(con, codes)
        d -= timedelta(days=1)
    return added_days


def _fallback_latest_prices(con, codes) -> bool:
    got = False
    for name, mod in (("TWSE-OpenAPI", twse), ("TPEX-OpenAPI", tpex)):
        try:
            res = mod.fetch_prices_latest()
            if res:
                _, rows = res
                db.upsert_many(con, "prices",
                               ["code", "date", "open", "high", "low", "close", "volume"],
                               [r for r in rows if r[0] in codes])
                got = True
            db.record_health(con, "每日股價(備援)", name, True, "")
        except Exception as e:
            db.record_health(con, "每日股價(備援)", name, False, str(e))
    return got


def repair_partial_days(con, progress=None) -> list[str]:
    """找出筆數異常偏低的日期（單邊來源失敗造成的不完整日）並重抓。"""
    codes = _universe_codes(con)
    repaired = []
    for table, thr, fetchers, cols in (
        # 股價每日檔數穩定，門檻收緊；法人/融資天然隨市況波動，門檻放寬
        ("prices", 0.95,
         (("TWSE", twse.fetch_prices_by_date), ("TPEX", tpex.fetch_prices_by_date)),
         ["code", "date", "open", "high", "low", "close", "volume"]),
        ("insti", 0.90,
         (("TWSE", twse.fetch_insti_by_date), ("TPEX", tpex.fetch_insti_by_date)),
         ["code", "date", "foreign_net", "trust_net", "dealer_net", "total_net"]),
        ("margin", 0.90,
         (("TWSE", twse.fetch_margin_by_date), ("TPEX", tpex.fetch_margin_by_date)),
         ["code", "date", "fin_balance", "fin_chg", "short_balance", "short_chg"]),
    ):
        rows = con.execute(f"SELECT date, COUNT(*) FROM {table} GROUP BY date").fetchall()
        if len(rows) < 10:
            continue
        counts = sorted(n for _, n in rows)
        med = counts[len(counts) // 2]
        bad = [d for d, n in rows if n < med * thr]
        for iso in bad:
            ymd = iso.replace("-", "")
            day_rows = []
            ok = True
            for name, fn in fetchers:
                try:
                    r = fn(ymd)
                    if r:
                        day_rows += [x for x in r if x[0] in codes]
                except Exception as e:
                    ok = False
                    db.record_health(con, f"完整性修復({table})", name, False,
                                     f"{iso}: {e}")
            old_n = con.execute(f"SELECT COUNT(*) FROM {table} WHERE date=?",
                                (iso,)).fetchone()[0]
            if ok and len(day_rows) > old_n:
                con.execute(f"DELETE FROM {table} WHERE date=?", (iso,))
                db.upsert_many(con, table, cols, day_rows)
                repaired.append(f"{table} {iso}: {old_n}→{len(day_rows)}")
                db.record_health(con, f"完整性修復({table})", "TWSE+TPEX", True,
                                 f"{iso}: {old_n}→{len(day_rows)} 筆")
            _log(progress, f"完整性修復 {table} {iso}（{old_n}→{len(day_rows)} 筆）")
    return repaired


# ---------- 三大法人（逐日回補） ----------

def backfill_insti(con, n_trading_days: int = 15, progress=None) -> int:
    codes = _universe_codes(con)
    have = set(db.insti_dates(con))
    counted = 0
    added = 0
    d = date.today()
    scanned = 0
    while counted < n_trading_days and scanned < n_trading_days * 2 + 30:
        scanned += 1
        if d.weekday() >= 5:
            d -= timedelta(days=1)
            continue
        iso = d.isoformat()
        ymd = d.strftime("%Y%m%d")
        if iso in have:
            counted += 1
            d -= timedelta(days=1)
            continue
        day_rows = []
        for name, mod in (("TWSE", twse), ("TPEX", tpex)):
            try:
                rows = mod.fetch_insti_by_date(ymd)
                if rows:
                    day_rows += [r for r in rows if r[0] in codes]
                db.record_health(con, "三大法人", name, True,
                                 f"{iso} {'有' if rows else '休市/無'}資料")
            except Exception as e:
                db.record_health(con, "三大法人", name, False, f"{iso}: {e}")
        if day_rows:
            db.upsert_many(con, "insti",
                           ["code", "date", "foreign_net", "trust_net", "dealer_net",
                            "total_net"], day_rows)
            counted += 1
            added += 1
            _log(progress, f"法人資料已回補 {iso}", min(counted / n_trading_days, 1.0))
        d -= timedelta(days=1)
    return added


# ---------- 融資融券（逐日回補） ----------

def backfill_margin(con, n_trading_days: int = 15, progress=None) -> int:
    codes = _universe_codes(con)
    have = set(db.margin_dates(con))
    counted = added = scanned = 0
    d = date.today()
    while counted < n_trading_days and scanned < n_trading_days * 2 + 30:
        scanned += 1
        if d.weekday() >= 5:
            d -= timedelta(days=1)
            continue
        iso = d.isoformat()
        ymd = d.strftime("%Y%m%d")
        if iso in have:
            counted += 1
            d -= timedelta(days=1)
            continue
        day_rows = []
        for name, mod in (("TWSE", twse), ("TPEX", tpex)):
            try:
                rows = mod.fetch_margin_by_date(ymd)
                if rows:
                    day_rows += [r for r in rows if r[0] in codes]
                db.record_health(con, "融資融券", name, True,
                                 f"{iso} {'有' if rows else '休市/無'}資料")
            except Exception as e:
                db.record_health(con, "融資融券", name, False, f"{iso}: {e}")
        if day_rows:
            db.upsert_many(con, "margin",
                           ["code", "date", "fin_balance", "fin_chg",
                            "short_balance", "short_chg"], day_rows)
            counted += 1
            added += 1
            _log(progress, f"融資融券已回補 {iso}", min(counted / n_trading_days, 1.0))
        d -= timedelta(days=1)
    return added


# ---------- 加權指數 ----------

def update_taiex(con, months: int = 4) -> int:
    total = 0
    d = date.today().replace(day=1)
    try:
        for _ in range(months):
            rows = twse.fetch_taiex_month(d.strftime("%Y%m%d"))
            total += db.upsert_many(con, "index_daily", ["date", "taiex"], rows)
            d = (d - timedelta(days=1)).replace(day=1)
        db.record_health(con, "加權指數", "TWSE-FMTQIK", True, f"{total} 日")
    except Exception as e:
        db.record_health(con, "加權指數", "TWSE-FMTQIK", False, str(e))
    return total


# ---------- 月營收歷史回補 ----------

def backfill_revenue_history(con, months: int = 24, progress=None) -> int:
    """從 MOPS 歷史月檔回補營收（供「創 N 月新高」「連 N 月 YoY 正成長」篩選）。"""
    codes = _universe_codes(con)
    cols = ["code", "month", "revenue", "mom", "yoy", "cum_yoy"]
    y, m = mops.latest_month()
    total = 0
    for i in range(months):
        for market in ("sii", "otc"):
            try:
                rows = [r for r in mops.fetch_revenue(market, y, m) if r[0] in codes]
                total += db.upsert_many(con, "revenue", cols, rows)
            except Exception as e:
                db.record_health(con, "月營收歷史", f"MOPS-{market}", False,
                                 f"{y}-{m:02d}: {e}")
        _log(progress, f"營收歷史回補 {y}-{m:02d}（累計 {total} 筆）",
             (i + 1) / months)
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    db.record_health(con, "月營收歷史", "MOPS", True, f"回補 {months} 個月共 {total} 筆")
    return total


# ---------- 備份與修剪 ----------

def backup_db(con, keep: int = 4) -> str | None:
    """每週一次用 VACUUM INTO 產生一致性快照到 data/backup/。"""
    last = db.get_meta(con, "last_backup")
    if last and (date.today() - date.fromisoformat(last)).days < 7:
        return None
    bdir = db.DATA_DIR / "backup"
    bdir.mkdir(exist_ok=True)
    target = bdir / f"screener_{date.today():%Y%m%d}.db"
    if target.exists():
        target.unlink()
    con.execute("VACUUM INTO ?", (str(target),))
    db.set_meta(con, "last_backup", date.today().isoformat())
    olds = sorted(bdir.glob("screener_*.db"))
    for f in olds[:-keep]:
        f.unlink()
    return str(target)


def prune_old_data(con, keep_days: int = 750) -> int:
    cutoff = (date.today() - timedelta(days=keep_days)).isoformat()
    n = 0
    for table in ("prices", "insti", "margin"):
        n += con.execute(f"DELETE FROM {table} WHERE date < ?", (cutoff,)).rowcount
    con.commit()
    return n


# ---------- 集保股權分散（每週） ----------

def update_tdcc(con, progress=None) -> str | None:
    try:
        res = tdcc.fetch_latest()
        if not res:
            db.record_health(con, "集保股權分散", "TDCC", False, "無資料")
            return None
        date_iso, rows = res
        codes = _universe_codes(con)
        db.upsert_many(con, "tdcc",
                       ["code", "date", "level", "holders", "shares", "pct"],
                       [r for r in rows if r[0] in codes])
        db.record_health(con, "集保股權分散", "TDCC", True, f"最新週：{date_iso}")
        _log(progress, f"集保資料更新完成（{date_iso}）")
        return date_iso
    except Exception as e:
        db.record_health(con, "集保股權分散", "TDCC", False, str(e))
        return None


def backfill_tdcc_history(con, codes: list[str], weeks: int = 8,
                          progress=None) -> tuple[int, str | None]:
    """用 FinMind 逐檔回補集保歷史（需免費註冊 token；額度有限，建議只補概念股）。
    回傳 (成功檔數, 錯誤訊息或 None)。"""
    token = db.get_meta(con, "finmind_token") or None
    if not token:
        msg = "需要 FinMind API token（免費）：請至 finmindtrade.com 註冊，於設定頁填入"
        db.record_health(con, "集保歷史(備援)", "FinMind", False, msg)
        return 0, msg
    start = (date.today() - timedelta(weeks=weeks + 1)).isoformat()
    done = 0
    err = None
    for i, code in enumerate(codes):
        try:
            rows = finmind.fetch_stock_tdcc(code, start, token)
            if rows:
                db.upsert_many(con, "tdcc",
                               ["code", "date", "level", "holders", "shares", "pct"], rows)
            done += 1
            db.record_health(con, "集保歷史(備援)", "FinMind", True, f"已回補 {done} 檔")
        except finmind.FinMindAuthError as e:
            err = str(e)
            db.record_health(con, "集保歷史(備援)", "FinMind", False, err)
            break  # token 無效或額度用盡，不再嘗試
        except Exception as e:
            err = str(e)
            db.record_health(con, "集保歷史(備援)", "FinMind", False, err)
        _log(progress, f"集保歷史回補 {code}（{i + 1}/{len(codes)}）",
             (i + 1) / len(codes))
    return done, err


# ---------- 基本面：月營收 + 估值 ----------

def update_fundamentals(con, progress=None) -> dict:
    codes = _universe_codes(con)
    result = {}
    rev_cols = ["code", "month", "revenue", "mom", "yoy", "cum_yoy"]

    # 月營收：主來源 MOPS 完整彙總檔（OpenAPI 檔缺台積電等約 60 檔，僅作備援）
    total = 0
    y, m = mops.latest_month()
    for market, name in (("sii", "MOPS-上市"), ("otc", "MOPS-上櫃")):
        try:
            rows = [r for r in mops.fetch_revenue(market, y, m) if r[0] in codes]
            if not rows and m > 1:  # 當月檔尚未產出 → 退一個月
                rows = [r for r in mops.fetch_revenue(market, y, m - 1)
                        if r[0] in codes]
            total += db.upsert_many(con, "revenue", rev_cols, rows)
            db.record_health(con, "月營收", name, bool(rows), f"{len(rows)} 筆")
        except Exception as e:
            db.record_health(con, "月營收", name, False, str(e))
    if total < 1000:  # MOPS 失敗時退回 OpenAPI
        for name, fn in (("TWSE-OpenAPI", twse.fetch_revenue),
                         ("TPEX-OpenAPI", tpex.fetch_revenue)):
            try:
                rows = [r for r in fn() if r[0] in codes]
                total += db.upsert_many(con, "revenue", rev_cols, rows)
                db.record_health(con, "月營收(備援)", name, True, f"{len(rows)} 筆")
            except Exception as e:
                db.record_health(con, "月營收(備援)", name, False, str(e))
    result["月營收"] = total
    _log(progress, f"月營收更新完成（{total} 筆）")

    # 估值：本益比／殖利率／股價淨值比
    total = 0
    for name, fn in (("TWSE", twse.fetch_valuation), ("TPEX", tpex.fetch_valuation)):
        try:
            rows = [r for r in fn() if r[0] in codes]
            total += db.upsert_many(con, "valuation",
                                    ["code", "date", "pe", "dividend_yield", "pb"], rows)
            db.record_health(con, "估值", name, True, f"{len(rows)} 筆")
        except Exception as e:
            db.record_health(con, "估值", name, False, str(e))
    result["估值"] = total
    _log(progress, f"估值更新完成（{total} 筆）")
    return result


# ---------- 一鍵更新 ----------

def update_all(con, price_days: int = 60, insti_days: int = 15, progress=None) -> dict:
    result = {}
    updated = db.get_meta(con, "universe_updated")
    if not updated or (date.today() - date.fromisoformat(updated)).days >= 7 \
            or not _universe_codes(con):
        _log(progress, "更新股票清單…", 0.02)
        result["universe"] = update_universe(con, progress)
    _log(progress, "回補每日股價…", 0.05)
    result["price_days"] = backfill_prices(con, price_days, progress)
    _log(progress, "回補三大法人…", 0.7)
    result["insti_days"] = backfill_insti(con, insti_days, progress)
    _log(progress, "回補融資融券…", 0.8)
    result["margin_days"] = backfill_margin(con, min(insti_days, 20), progress)
    _log(progress, "檢查資料完整性…", 0.85)
    result["repaired"] = repair_partial_days(con, progress)
    _log(progress, "更新月營收與估值…", 0.88)
    result["fundamentals"] = update_fundamentals(con, progress)
    _log(progress, "更新加權指數…", 0.93)
    result["taiex"] = update_taiex(con)
    _log(progress, "更新集保股權分散…", 0.95)
    result["tdcc"] = update_tdcc(con, progress)
    db.set_meta(con, "last_update", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    _log(progress, "全部完成", 1.0)
    return result


# ---------- 盤中即時 ----------

def live_quotes(con) -> dict[str, dict]:
    rows = con.execute("SELECT code, market FROM stocks").fetchall()
    try:
        quotes = mis.fetch_quotes(rows)
        db.record_health(con, "盤中即時報價", "TWSE-MIS", True, f"{len(quotes)} 檔")
        return quotes
    except Exception as e:
        db.record_health(con, "盤中即時報價", "TWSE-MIS", False, str(e))
        return {}
