"""篩選引擎：均線＋技術指標＋法人/大戶籌碼＋基本面＋概念股標籤。

篩選條件以 params dict 傳遞（與推播設定、回測共用同一組鍵）：
  require_above, require_bullish,
  kd_cross, macd_flip, bb_break, rsi_range=(lo,hi), bias_max,
  foreign_days, trust_days, total_buy, big_increase, retail_decrease,
  rev_yoy_min(-100=off), yield_min(0=off), pe_max(0=off),
  min_volume_lot, concepts_sel=[...], watchlist_codes=set|None
"""
from datetime import date

import pandas as pd

from . import db, indicators
from .sources.tdcc import RETAIL_MAX_LEVEL, THRESHOLD_LEVEL


def _streak(series: pd.Series) -> int:
    n = 0
    for v in series.iloc[::-1]:
        if v > 0:
            n += 1
        else:
            break
    return n


def build_table(con, ma_periods=(5, 10, 20), live: dict | None = None,
                big_threshold: int = 400,
                prices_df: pd.DataFrame | None = None) -> pd.DataFrame:
    """全市場指標總表（尚未套用篩選）。live 為 MIS 即時報價 dict；
    prices_df 可傳入快取好的價格資料避免重複讀 DB。"""
    max_p = max(ma_periods)
    # 指標暖身 + RS 需要 91 日
    prices = prices_df if prices_df is not None \
        else db.load_prices(con, max(max_p + 5, 100))
    if prices.empty:
        return pd.DataFrame()

    pv = indicators.make_pivots(prices)

    if live:
        live_price = pd.Series({c: q["price"] for c, q in live.items()
                                if q.get("price")})
        today = date.today().isoformat()
        for key in ("close", "high", "low"):
            piv = pv[key]
            row = piv.iloc[-1].copy()
            row.update(live_price)
            if piv.index[-1] == today:
                piv.iloc[-1] = row
            elif date.today().weekday() < 5:
                piv.loc[today] = row
                pv[key] = piv.sort_index()

    cpiv = pv["close"]
    last_date = cpiv.index[-1]
    close = cpiv.iloc[-1]
    prev = cpiv.iloc[-2] if len(cpiv) >= 2 else close
    out = pd.DataFrame({"close": close, "prev_close": prev})
    out["change_pct"] = ((close - prev) / prev * 100).round(2)

    for p in ma_periods:
        out[f"ma{p}"] = cpiv.rolling(p).mean().iloc[-1].round(2)
    out["above_all"] = True
    for p in ma_periods:
        out["above_all"] &= close > out[f"ma{p}"]
    ps = sorted(ma_periods)
    out["bullish"] = True
    for a, b in zip(ps, ps[1:]):
        out["bullish"] &= out[f"ma{a}"] > out[f"ma{b}"]

    # ---- 技術指標（取最新值與訊號）----
    ind = indicators.compute(pv)
    out["k9"] = ind["k"].iloc[-1].round(1)
    out["d9"] = ind["d"].iloc[-1].round(1)
    out["rsi14"] = ind["rsi"].iloc[-1].round(1)
    out["macd_hist"] = ind["macd_hist"].iloc[-1].round(3)
    out["bias20"] = ind["bias20"].iloc[-1].round(2)
    if len(cpiv) >= 2:
        out["kd_cross"] = (ind["k"].iloc[-1] > ind["d"].iloc[-1]) & \
                          (ind["k"].iloc[-2] <= ind["d"].iloc[-2])
        out["macd_flip"] = (ind["macd_hist"].iloc[-1] > 0) & \
                           (ind["macd_hist"].iloc[-2] <= 0)
    else:
        out["kd_cross"] = False
        out["macd_flip"] = False
    out["bb_break"] = close > ind["bb_up"].iloc[-1]

    # ---- 相對強弱 RS：90 日報酬的全市場百分位（0-100）----
    lookback = min(90, len(cpiv) - 1)
    if lookback >= 20:
        ret_n = close / cpiv.iloc[-lookback - 1] - 1
        out["rs"] = (ret_n.rank(pct=True) * 100).round(1)
    else:
        out["rs"] = pd.NA

    vol = prices[prices["date"] == prices["date"].max()].set_index("code")["volume"]
    out["volume_lot"] = (vol / 1000).round(0)
    if live:
        lv = pd.Series({c: q["volume"] for c, q in live.items() if q.get("volume")})
        out.loc[lv.index.intersection(out.index), "volume_lot"] = lv

    # ---- 三大法人 ----
    insti = db.load_insti(con, 12)
    if not insti.empty:
        insti = insti.sort_values("date")
        last_i = insti[insti["date"] == insti["date"].max()].set_index("code")
        for col, name in (("foreign_net", "foreign"), ("trust_net", "trust"),
                          ("dealer_net", "dealer"), ("total_net", "total")):
            out[f"{name}_net"] = (last_i[col] / 1000).round(0)
        g = insti.groupby("code")
        out["foreign_streak"] = g["foreign_net"].apply(_streak)
        out["trust_streak"] = g["trust_net"].apply(_streak)
    else:
        for c in ("foreign_net", "trust_net", "dealer_net", "total_net",
                  "foreign_streak", "trust_streak"):
            out[c] = pd.NA

    # ---- 集保大戶/散戶 ----
    t = db.load_tdcc(con, 2)
    out["big_pct"] = pd.NA
    out["big_pct_chg"] = pd.NA
    out["retail_pct"] = pd.NA
    out["retail_pct_chg"] = pd.NA
    out["holders"] = pd.NA
    out["tdcc_date"] = None
    if not t.empty:
        min_level = THRESHOLD_LEVEL.get(big_threshold, 12)
        dates = sorted(t["date"].unique())
        cur = t[t["date"] == dates[-1]]
        big = cur[(cur["level"] >= min_level) & (cur["level"] <= 15)] \
            .groupby("code")["pct"].sum().round(2)
        retail = cur[cur["level"] <= RETAIL_MAX_LEVEL] \
            .groupby("code")["pct"].sum().round(2)
        holders = cur[cur["level"] == 17].set_index("code")["holders"]
        out["big_pct"] = big
        out["retail_pct"] = retail
        out["holders"] = holders
        out["tdcc_date"] = dates[-1]
        if len(dates) >= 2:
            prev_w = t[t["date"] == dates[-2]]
            big_p = prev_w[(prev_w["level"] >= min_level) & (prev_w["level"] <= 15)] \
                .groupby("code")["pct"].sum()
            retail_p = prev_w[prev_w["level"] <= RETAIL_MAX_LEVEL] \
                .groupby("code")["pct"].sum()
            out["big_pct_chg"] = (big - big_p).round(2)
            out["retail_pct_chg"] = (retail - retail_p).round(2)

    # ---- 融資融券 ----
    mg = db.load_margin(con, 12)
    if not mg.empty:
        mg = mg.sort_values("date")
        last_m = mg[mg["date"] == mg["date"].max()].set_index("code")
        out["fin_balance"] = last_m["fin_balance"]
        out["fin_chg"] = last_m["fin_chg"]
        out["short_balance"] = last_m["short_balance"]
        g = mg.groupby("code")
        out["fin_down_streak"] = g["fin_chg"].apply(lambda s: _streak(-s))
    else:
        for c in ("fin_balance", "fin_chg", "short_balance", "fin_down_streak"):
            out[c] = pd.NA

    # ---- 基本面：月營收 + 估值 ----
    rev = db.load_latest_revenue(con)
    if not rev.empty:
        out["rev_yoy"] = rev["yoy"].round(1)
        out["rev_mom"] = rev["mom"].round(1)
        out["rev_month"] = rev["month"]
    else:
        out["rev_yoy"] = pd.NA
        out["rev_mom"] = pd.NA
        out["rev_month"] = None

    # ---- 營收動能（需先回補營收歷史）----
    rvm = db.load_revenue_matrix(con, 25)
    out["rev_high_streak"] = pd.NA   # 當月營收為近 N 月最高的最大 N
    out["rev_yoy_streak"] = pd.NA    # 連續 YoY>0 的月數（含當月）
    if not rvm.empty and rvm["month"].nunique() >= 3:
        rev_p = rvm.pivot_table(index="month", columns="code", values="revenue",
                                aggfunc="last").sort_index()
        yoy_p = rvm.pivot_table(index="month", columns="code", values="yoy",
                                aggfunc="last").sort_index()
        cur = rev_p.iloc[-1]
        # 從上個月往回數「連續 <= 當月」的月數：streak=k 代表當月營收創 k 月新高
        high_streak = pd.Series(1.0, index=rev_p.columns)
        alive = cur.notna()
        for i in range(len(rev_p) - 2, -1, -1):
            row = rev_p.iloc[i]
            alive &= row.notna() & (cur >= row)
            high_streak = high_streak.where(~alive, high_streak + 1)
        out["rev_high_streak"] = high_streak.where(cur.notna())
        # 從當月往回數「連續 YoY > 0」的月數
        yoy_streak = pd.Series(0.0, index=yoy_p.columns)
        alive = pd.Series(True, index=yoy_p.columns)
        for i in range(len(yoy_p) - 1, -1, -1):
            alive &= (yoy_p.iloc[i] > 0).fillna(False)
            yoy_streak = yoy_streak.where(~alive, yoy_streak + 1)
        out["rev_yoy_streak"] = yoy_streak.where(yoy_p.iloc[-1].notna())
    val = db.load_latest_valuation(con)
    if not val.empty:
        out["pe"] = val["pe"]
        out["dyield"] = val["dividend_yield"]
        out["pb"] = val["pb"]
    else:
        out["pe"] = pd.NA
        out["dyield"] = pd.NA
        out["pb"] = pd.NA

    # ---- 基本資料 ----
    stocks = db.universe(con).set_index("code")
    out = out.join(stocks[["name", "market", "industry_code", "industry_name"]],
                   how="inner")
    out["is_electronic"] = out["industry_code"].isin(db.ELECTRONIC_CODES)
    out.index.name = "code"
    out = out.reset_index()
    out["last_date"] = last_date
    return out


def apply_filters(df: pd.DataFrame, p: dict,
                  concept_map: dict[str, set] | None = None) -> pd.DataFrame:
    if df.empty:
        return df
    m = pd.Series(True, index=df.index)
    if p.get("require_above", True):
        m &= df["above_all"].fillna(False)
    if p.get("require_bullish"):
        m &= df["bullish"].fillna(False)
    # 技術指標
    if p.get("kd_cross"):
        m &= df["kd_cross"].fillna(False)
    if p.get("macd_flip"):
        m &= df["macd_flip"].fillna(False)
    if p.get("bb_break"):
        m &= df["bb_break"].fillna(False)
    rsi_range = tuple(p.get("rsi_range") or (0, 100))
    if rsi_range != (0, 100):
        m &= df["rsi14"].between(*rsi_range)
    if (p.get("bias_max") or 0) > 0:
        m &= df["bias20"].fillna(999) <= p["bias_max"]
    # 籌碼
    if p.get("foreign_days", 0) > 0:
        m &= df["foreign_streak"].fillna(0) >= p["foreign_days"]
    if p.get("trust_days", 0) > 0:
        m &= df["trust_streak"].fillna(0) >= p["trust_days"]
    if p.get("total_buy"):
        m &= df["total_net"].fillna(0) > 0
    if p.get("big_increase"):
        m &= df["big_pct_chg"].fillna(-1) > 0
    if p.get("retail_decrease"):
        m &= df["retail_pct_chg"].fillna(1) < 0
    # 動能與籌碼進階
    if (p.get("rs_min") or 0) > 0:
        m &= df["rs"].fillna(0) >= p["rs_min"]
    if (p.get("fin_down_days") or 0) > 0:
        m &= df["fin_down_streak"].fillna(0) >= p["fin_down_days"]
    if (p.get("rev_high_months") or 0) > 0:
        m &= df["rev_high_streak"].fillna(0) >= p["rev_high_months"]
    if (p.get("rev_yoy_pos_months") or 0) > 0:
        m &= df["rev_yoy_streak"].fillna(0) >= p["rev_yoy_pos_months"]
    # 基本面
    if (p.get("rev_yoy_min") if p.get("rev_yoy_min") is not None else -100) > -100:
        m &= df["rev_yoy"].fillna(-999) >= p["rev_yoy_min"]
    if (p.get("yield_min") or 0) > 0:
        m &= df["dyield"].fillna(0) >= p["yield_min"]
    if (p.get("pe_max") or 0) > 0:
        m &= df["pe"].fillna(9999).between(0.01, p["pe_max"])
    # 其他
    if (p.get("min_volume_lot") or 0) > 0:
        m &= df["volume_lot"].fillna(0) >= p["min_volume_lot"]
    wl = p.get("watchlist_codes")
    if wl:
        m &= df["code"].isin(wl)
    sel = p.get("concepts_sel") or []
    if sel:
        cm = concept_map or {}
        cmask = pd.Series(False, index=df.index)
        for tag in sel:
            if tag == "電子":
                cmask |= df["is_electronic"]
            else:
                cmask |= df["code"].isin(cm.get(tag, set()))
        m &= cmask
    return df[m]


def tag_concepts(df: pd.DataFrame, concept_map: dict[str, set]) -> pd.DataFrame:
    if df.empty:
        return df

    def tags(row):
        t = [name for name, codes in concept_map.items() if row["code"] in codes]
        if row.get("is_electronic"):
            t.append("電子")
        return "、".join(t)

    df = df.copy()
    df["concepts"] = df.apply(tags, axis=1)
    return df
