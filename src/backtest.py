"""回測引擎。

Phase 1（run）：條件勝率統計 — 每個歷史日重演篩選，統計 N 日後報酬分布（重疊持有）。
Phase 2（run_strategy）：完整策略回測 — 非重疊持有、次日開盤進場、停損停利、
交易成本、排除漲停無法買進、資金曲線與最大回檔。
"""
import numpy as np
import pandas as pd

from . import db, indicators

WARMUP = 91  # RS 需 90 日 + MACD 暖身

# 歷史深度不足、無法納入回測的條件
UNSUPPORTED = {
    "big_increase": "大戶比例週增", "retail_decrease": "散戶比例週減",
    "rev_high_months": "營收創N月新高", "rev_yoy_pos_months": "營收YoY連續成長",
    "fin_down_days": "融資連N日減",
}
ROUNDTRIP_COST = 0.585 / 100  # 手續費 0.1425%×2 + 證交稅 0.3%


def _build_mask(con, params: dict, ma_periods, concept_map, pv, ind):
    """組合所有可回測條件 → (布林觸發矩陣, 被跳過的條件名單)。"""
    c = pv["close"]
    cond_m = indicators.condition_matrices(pv, ind, ma_periods)

    mask = cond_m["above_all"].fillna(False) if params.get("require_above", True) \
        else pd.DataFrame(True, index=c.index, columns=c.columns)
    if params.get("require_bullish"):
        mask &= cond_m["bullish"].fillna(False)
    if params.get("kd_cross"):
        mask &= cond_m["kd_cross"].fillna(False)
    if params.get("macd_flip"):
        mask &= cond_m["macd_flip"].fillna(False)
    if params.get("bb_break"):
        mask &= cond_m["bb_break"].fillna(False)
    rsi_range = tuple(params.get("rsi_range") or (0, 100))
    if rsi_range != (0, 100):
        mask &= ind["rsi"].between(*rsi_range)
    if (params.get("bias_max") or 0) > 0:
        mask &= ind["bias20"] <= params["bias_max"]
    if (params.get("min_volume_lot") or 0) > 0:
        mask &= cond_m["volume_lot"].fillna(0) >= params["min_volume_lot"]
    if (params.get("rs_min") or 0) > 0:
        rs = (c / c.shift(90) - 1).rank(axis=1, pct=True) * 100
        mask &= rs >= params["rs_min"]

    used_insti = params.get("foreign_days", 0) > 0 or params.get("trust_days", 0) > 0 \
        or params.get("total_buy")
    if used_insti:
        insti = db.load_insti(con, 10_000)
        fg = insti.pivot_table(index="date", columns="code", values="foreign_net",
                               aggfunc="last").reindex(index=c.index, columns=c.columns)
        tr = insti.pivot_table(index="date", columns="code", values="trust_net",
                               aggfunc="last").reindex(index=c.index, columns=c.columns)
        tt = insti.pivot_table(index="date", columns="code", values="total_net",
                               aggfunc="last").reindex(index=c.index, columns=c.columns)
        n = params.get("foreign_days", 0)
        if n > 0:
            mask &= ((fg > 0).rolling(n).sum() == n)
        n = params.get("trust_days", 0)
        if n > 0:
            mask &= ((tr > 0).rolling(n).sum() == n)
        if params.get("total_buy"):
            mask &= (tt > 0)
        coverage = fg.notna().any(axis=1)
        mask = mask.loc[coverage[coverage].index.intersection(mask.index)]

    col_mask = pd.Series(True, index=c.columns)
    sel = params.get("concepts_sel") or []
    if sel and concept_map is not None:
        stocks = db.universe(con).set_index("code")
        cm = pd.Series(False, index=c.columns)
        for tag in sel:
            if tag == "電子":
                elec = stocks["industry_code"].isin(db.ELECTRONIC_CODES)
                cm |= elec.reindex(c.columns).fillna(False)
            else:
                cm |= pd.Series(c.columns.isin(concept_map.get(tag, set())),
                                index=c.columns)
        col_mask &= cm
    wl_codes = params.get("watchlist_codes")
    if wl_codes:
        col_mask &= pd.Series(c.columns.isin(wl_codes), index=c.columns)
    mask = mask.loc[:, col_mask[col_mask].index]
    mask = mask.iloc[WARMUP:]

    skipped = [name for key, name in UNSUPPORTED.items() if params.get(key)]
    return mask, skipped


def run(con, params: dict, ma_periods=(5, 10, 20), horizons=(5, 10, 20),
        concept_map: dict[str, set] | None = None) -> dict:
    """Phase 1：條件勝率統計（重疊持有）。"""
    prices = db.load_prices(con, 10_000)
    if prices.empty:
        return {"error": "無股價資料"}
    pv = indicators.make_pivots(prices)
    c = pv["close"]
    if len(c) < WARMUP + max(horizons) + 5:
        return {"error": f"歷史資料不足（現有 {len(c)} 個交易日，"
                         f"至少需要 {WARMUP + max(horizons) + 5} 個），"
                         "請先在「資料與設定」執行一年歷史回補"}
    ind = indicators.compute(pv)
    mask, skipped = _build_mask(con, params, ma_periods, concept_map, pv, ind)

    result = {"horizons": {}, "skipped": skipped,
              "date_range": (str(mask.index[0]), str(mask.index[-1])),
              "n_days": len(mask)}
    csub = c.loc[:, mask.columns]
    for h in horizons:
        fwd = csub.shift(-h) / csub - 1
        m_h = mask.iloc[:-h] if h > 0 else mask
        fwd_h = fwd.reindex(index=m_h.index)
        vals = fwd_h.where(m_h).stack().dropna()
        bench = fwd.loc[m_h.index].stack().dropna()
        if len(vals) == 0:
            result["horizons"][h] = {"n": 0}
            continue
        result["horizons"][h] = {
            "n": int(len(vals)),
            "win_rate": float((vals > 0).mean() * 100),
            "mean": float(vals.mean() * 100),
            "median": float(vals.median() * 100),
            "bench_mean": float(bench.mean() * 100),
            "bench_win": float((bench > 0).mean() * 100),
        }
        if h == max(horizons):
            result["returns_sample"] = (vals * 100).clip(-40, 60)
    result["triggers_per_day"] = mask.sum(axis=1)
    return result


def run_strategy(con, params: dict, ma_periods=(5, 10, 20),
                 concept_map: dict[str, set] | None = None, *,
                 entry: str = "next_open", hold_days: int = 20,
                 stop_loss: float = 7.0, take_profit: float = 0.0,
                 cost: bool = True, exclude_limit_up: bool = True) -> dict:
    """Phase 2：完整策略回測（非重疊持有）。"""
    prices = db.load_prices(con, 10_000)
    if prices.empty:
        return {"error": "無股價資料"}
    pv = indicators.make_pivots(prices)
    c, o, hi_, lo_ = pv["close"], pv["open"], pv["high"], pv["low"]
    if len(c) < WARMUP + hold_days + 5:
        return {"error": "歷史資料不足，請先回補一年歷史"}
    ind = indicators.compute(pv)
    mask, skipped = _build_mask(con, params, ma_periods, concept_map, pv, ind)

    dates = list(c.index)
    date_pos = {d: i for i, d in enumerate(dates)}
    fee = ROUNDTRIP_COST if cost else 0.0
    sl = stop_loss / 100 if stop_loss > 0 else None
    tp = take_profit / 100 if take_profit > 0 else None

    trades = []
    # 資金曲線：每日所有在倉交易的平均日報酬
    day_ret_sum = np.zeros(len(dates))
    day_ret_cnt = np.zeros(len(dates))

    for code in mask.columns:
        trig = mask.index[mask[code].fillna(False)]
        if len(trig) == 0:
            continue
        ca = c[code].to_numpy()
        oa = o[code].to_numpy()
        ha = hi_[code].to_numpy()
        la = lo_[code].to_numpy()
        next_free = 0
        for d_ in trig:
            ti = date_pos[d_]
            if ti < next_free:
                continue  # 仍在前一筆持有期內 → 非重疊
            if exclude_limit_up and ti > 0 and ca[ti] == ca[ti]:
                prev = ca[ti - 1]
                if prev == prev and ca[ti] >= prev * 1.0945:
                    continue  # 觸發日收漲停，隔日大概率買不到
            ei = ti + 1 if entry == "next_open" else ti
            if ei >= len(dates):
                continue
            entry_px = oa[ei] if entry == "next_open" else ca[ti]
            if entry_px != entry_px or entry_px <= 0:
                continue
            stop_px = entry_px * (1 - sl) if sl else None
            tp_px = entry_px * (1 + tp) if tp else None
            exit_px = exit_i = None
            reason = "到期"
            held = 0
            j = ei
            while j < len(dates):
                if ca[j] != ca[j]:  # 停牌日不計持有天數
                    j += 1
                    continue
                held += 1
                if stop_px and la[j] == la[j] and la[j] <= stop_px:
                    # 跳空低開直接以開盤成交，否則以停損價
                    exit_px = min(stop_px, oa[j]) if oa[j] == oa[j] else stop_px
                    exit_i, reason = j, "停損"
                    break
                if tp_px and ha[j] == ha[j] and ha[j] >= tp_px:
                    exit_px = max(tp_px, oa[j]) if oa[j] == oa[j] else tp_px
                    exit_i, reason = j, "停利"
                    break
                if held >= hold_days:
                    exit_px, exit_i = ca[j], j
                    break
                j += 1
            if exit_px is None:
                continue  # 資料末端未出場的交易不計
            ret = exit_px / entry_px - 1 - fee
            trades.append({"code": code, "entry_date": dates[ei],
                           "exit_date": dates[exit_i], "entry": round(entry_px, 2),
                           "exit": round(exit_px, 2), "ret_pct": round(ret * 100, 2),
                           "days": held, "reason": reason})
            # 資金曲線：持有期間逐日報酬（首日相對進場價，出場日用出場價）
            prev_px = entry_px
            for k in range(ei, exit_i + 1):
                px = ca[k]
                if px != px:
                    continue
                if k == exit_i:
                    px = exit_px
                day_ret_sum[k] += px / prev_px - 1
                day_ret_cnt[k] += 1
                prev_px = px
            next_free = exit_i + 1

    if not trades:
        return {"error": "此條件在回測期間沒有任何可成交的交易", "skipped": skipped}

    tdf = pd.DataFrame(trades).sort_values("entry_date")
    rets = tdf["ret_pct"]
    wins, losses = rets[rets > 0], rets[rets <= 0]
    port_ret = np.where(day_ret_cnt > 0, day_ret_sum / np.maximum(day_ret_cnt, 1), 0.0)
    equity = pd.Series((1 + pd.Series(port_ret, index=dates)).cumprod(), index=dates)
    equity = equity.iloc[WARMUP:]
    dd = (equity / equity.cummax() - 1) * 100

    # 全市場等權基準（同期間）
    mkt_ret = (c / c.shift(1) - 1).mean(axis=1).fillna(0)
    bench = (1 + mkt_ret).cumprod()
    bench = bench.iloc[WARMUP:]
    bench = bench / bench.iloc[0] * equity.iloc[0]

    stats = {
        "n": len(tdf),
        "win_rate": float((rets > 0).mean() * 100),
        "mean": float(rets.mean()),
        "median": float(rets.median()),
        "profit_factor": float(wins.sum() / abs(losses.sum())) if len(losses) and losses.sum() != 0 else float("inf"),
        "avg_days": float(tdf["days"].mean()),
        "total_return": float((equity.iloc[-1] / equity.iloc[0] - 1) * 100),
        "bench_return": float((bench.iloc[-1] / bench.iloc[0] - 1) * 100),
        "max_drawdown": float(dd.min()),
        "reasons": tdf["reason"].value_counts().to_dict(),
    }
    return {"trades": tdf, "stats": stats, "equity": equity, "bench": bench,
            "drawdown": dd, "skipped": skipped,
            "date_range": (str(equity.index[0]), str(equity.index[-1]))}
