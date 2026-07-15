"""台股均線籌碼篩選器 — Streamlit 儀表板

啟動：py -m streamlit run app.py（或雙擊 啟動篩選器.bat）
"""
import json
import subprocess
import sys
import time
from datetime import date, datetime

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from src import backtest as bt
from src import cloud
from src import concepts as cp
from src import db, healthcheck, intraday, notify, screener, updater

st.set_page_config(page_title="台股均線籌碼篩選器", page_icon="📈", layout="wide")

CLOUD_URL = cloud.data_url()
IS_CLOUD = bool(CLOUD_URL)
if IS_CLOUD:
    sync_status = cloud.sync_db(CLOUD_URL)


def get_con():
    return db.connect()


con = get_con()
DB_EMPTY = len(db.price_dates(con)) == 0


# ══════════════════ 快取層 ══════════════════
def data_version() -> str:
    return db.get_meta(con, "last_update", "") or ""


@st.cache_data(ttl=300, show_spinner=False)
def cached_recent_prices(dv: str):
    return db.load_prices(db.connect(), 100)


@st.cache_data(ttl=60, show_spinner=False)
def cached_table(ma_periods: tuple, big_threshold: int, dv: str):
    return screener.build_table(db.connect(), ma_periods, None, big_threshold,
                                prices_df=cached_recent_prices(dv))


@st.cache_data(ttl=600, show_spinner=False)
def cached_live_quotes(tick: int, codes: tuple | None):
    con2 = db.connect()
    rows = con2.execute("SELECT code, market FROM stocks").fetchall()
    if codes is not None:
        cs = set(codes)
        rows = [r for r in rows if r[0] in cs]
    from src.sources import mis
    try:
        q = mis.fetch_quotes(rows)
        db.record_health(con2, "盤中即時報價", "TWSE-MIS", True, f"{len(q)} 檔")
        return q
    except Exception as e:
        db.record_health(con2, "盤中即時報價", "TWSE-MIS", False, str(e))
        return {}


@st.cache_data(ttl=3600, show_spinner=False)
def cached_quickstats(pjson: str, ma: tuple, dv: str):
    """篩選頁的歷史勝率標註（輕量版 Phase 1 回測）。"""
    p = json.loads(pjson)
    if p.get("watchlist_codes"):
        p["watchlist_codes"] = set(p["watchlist_codes"])
    res = bt.run(db.connect(), p, ma, concept_map=cp.as_sets())
    if res.get("error"):
        return {"error": res["error"]}
    return {"horizons": res["horizons"], "skipped": res["skipped"],
            "n_days": res["n_days"]}


def run_update(price_days, insti_days):
    bar = st.progress(0.0, text="準備中…")

    def cb(msg, frac=None):
        bar.progress(min(frac or 0.0, 1.0), text=msg)

    result = updater.update_all(db.connect(), price_days, insti_days, cb)
    bar.progress(1.0, text="完成")
    st.cache_data.clear()
    return result


# ══════════════════ 側邊欄：預設值與策略 ══════════════════
DEFAULTS = {
    "k_ma": [5, 10, 20], "k_bullish": False,
    "k_kd": False, "k_macd": False, "k_bb": False, "k_rsi": (0, 100),
    "k_bias": 0.0, "k_rs": 0,
    "k_fdays": 0, "k_tdays": 0, "k_totbuy": False, "k_bigthr": 400,
    "k_biginc": False, "k_retdec": False, "k_findown": 0,
    "k_revyoy": -100.0, "k_yield": 0.0, "k_pe": 0.0,
    "k_revhigh": 0, "k_yoystreak": 0,
    "k_concepts": [], "k_wl": "（全部股票）", "k_minvol": 500,
}
for _k, _v in DEFAULTS.items():
    st.session_state.setdefault(_k, _v)

PARAM_KEYS = {  # 策略儲存欄位 ↔ widget key
    "require_bullish": "k_bullish", "kd_cross": "k_kd", "macd_flip": "k_macd",
    "bb_break": "k_bb", "rsi_range": "k_rsi", "bias_max": "k_bias",
    "rs_min": "k_rs", "foreign_days": "k_fdays", "trust_days": "k_tdays",
    "total_buy": "k_totbuy", "big_increase": "k_biginc",
    "retail_decrease": "k_retdec", "fin_down_days": "k_findown",
    "rev_yoy_min": "k_revyoy", "yield_min": "k_yield", "pe_max": "k_pe",
    "rev_high_months": "k_revhigh", "rev_yoy_pos_months": "k_yoystreak",
    "concepts_sel": "k_concepts", "watchlist": "k_wl",
    "min_volume_lot": "k_minvol",
}


def apply_strategy(sdata: dict):
    p = sdata.get("params", {})
    for pk, wk in PARAM_KEYS.items():
        if pk in p and p[pk] is not None:
            v = p[pk]
            if wk == "k_rsi":
                v = tuple(v)
            if wk == "k_wl" and v not in ["（全部股票）"] + db.watchlist_names(con):
                v = "（全部股票）"
            st.session_state[wk] = v
        elif pk == "watchlist":
            st.session_state[wk] = "（全部股票）"
    st.session_state["k_ma"] = list(sdata.get("ma_periods", [5, 10, 20]))
    st.session_state["k_bigthr"] = sdata.get("big_threshold", 400)


with st.sidebar:
    st.title("📈 台股篩選器")
    last_update = db.get_meta(con, "last_update")
    if last_update:
        st.caption(f"上次資料更新：{last_update}")

    with st.expander("💾 策略（儲存／切換條件組合）"):
        snames = db.strategy_names(con)
        sel_st = st.selectbox("已存策略", ["（選擇策略）"] + snames)
        sc1, sc2 = st.columns(2)
        if sc1.button("套用", disabled=sel_st == "（選擇策略）",
                      use_container_width=True):
            raw = db.load_strategy(con, sel_st)
            if raw:
                apply_strategy(json.loads(raw))
                st.rerun()
        if sc2.button("刪除", disabled=sel_st == "（選擇策略）",
                      use_container_width=True):
            db.delete_strategy(con, sel_st)
            st.rerun()
        new_sname = st.text_input("另存目前條件為策略", placeholder="例如：外資動能")

    if IS_CLOUD:
        st.caption(f"☁️ 雲端版｜{sync_status}｜資料由主機每日盤後自動同步")
    live_mode = st.toggle("盤中即時價（MIS）", value=False,
                          disabled=DB_EMPTY or IS_CLOUD,
                          help="雲端版不支援（證交所即時介面限台灣網路）；"
                               "以即時報價重算均線與指標" if IS_CLOUD else
                               "以即時報價重算均線與指標；籌碼/基本面仍為盤後資料")
    refresh_min = st.slider("自動刷新間隔（分鐘）", 1, 10, 3, disabled=not live_mode)
    full_poll = st.checkbox("全市場完整輪詢", value=False, disabled=not live_mode,
                            help="預設只輪詢接近均線的候選股（快 3 倍）；勾選改為全市場")

    with st.expander("📏 均線條件", expanded=True):
        ma_choice = st.multiselect("站上均線（收盤價 > 均線）", [3, 5, 8, 10, 20, 60],
                                   key="k_ma")
        require_bullish = st.checkbox("均線多頭排列（短>長）", key="k_bullish")

    with st.expander("📐 技術指標與動能"):
        kd_cross = st.checkbox("KD 黃金交叉（今日 K 上穿 D）", key="k_kd")
        macd_flip = st.checkbox("MACD 柱狀圖翻紅（由負轉正）", key="k_macd")
        bb_break = st.checkbox("突破布林上軌（20, 2σ）", key="k_bb")
        rsi_range = st.slider("RSI(14) 範圍", 0, 100, key="k_rsi")
        bias_max = st.number_input("20日乖離率上限 %（0=不限）", 0.0, 100.0,
                                   step=1.0, key="k_bias")
        rs_min = st.slider("相對強弱 RS ≥（90日報酬全市場百分位，0=不限）",
                           0, 99, key="k_rs")

    with st.expander("💰 籌碼條件"):
        foreign_days = st.slider("外資連續買超 ≥ N 日", 0, 10, key="k_fdays")
        trust_days = st.slider("投信連續買超 ≥ N 日", 0, 10, key="k_tdays")
        total_buy = st.checkbox("當日三大法人合計買超", key="k_totbuy")
        fin_down_days = st.slider("融資連續減少 ≥ N 日（籌碼沉澱）", 0, 10,
                                  key="k_findown")
        big_threshold = st.selectbox("大戶門檻（張）", [100, 200, 400, 600, 800, 1000],
                                     key="k_bigthr")
        n_tdcc_weeks = len(db.tdcc_dates(con))
        big_inc = st.checkbox("大戶持股比例週增", key="k_biginc",
                              disabled=n_tdcc_weeks < 2)
        retail_dec = st.checkbox("散戶（≤10張）比例週減", key="k_retdec",
                                 disabled=n_tdcc_weeks < 2)

    with st.expander("📊 基本面與營收動能"):
        rev_yoy_min = st.number_input("月營收 YoY ≥ %（-100=不限）", -100.0, 500.0,
                                      step=5.0, key="k_revyoy")
        rev_high_months = st.slider("營收創 N 月新高（0=不限）", 0, 24, key="k_revhigh")
        rev_yoy_streak = st.slider("營收 YoY 連續 N 月正成長（0=不限）", 0, 12,
                                   key="k_yoystreak")
        yield_min = st.number_input("殖利率 ≥ %（0=不限）", 0.0, 20.0, step=0.5,
                                    key="k_yield")
        pe_max = st.number_input("本益比 ≤（0=不限）", 0.0, 200.0, step=1.0, key="k_pe")

    with st.expander("🏷️ 概念與自選股"):
        concept_data = cp.load()
        concept_options = list(concept_data.keys()) + ["電子"]
        concepts_sel = st.multiselect("僅顯示所選概念（留空 = 全部）", concept_options,
                                      key="k_concepts")
        wl_options = ["（全部股票）"] + db.watchlist_names(con)
        if st.session_state["k_wl"] not in wl_options:
            st.session_state["k_wl"] = "（全部股票）"
        wl_pick = st.selectbox("只看自選股清單", wl_options, key="k_wl")

    min_vol = st.number_input("最低成交量（張，0=不限）", 0, step=100, key="k_minvol")
    show_badge = st.checkbox("篩選結果顯示歷史勝率標註", value=True)

wl_codes = set(db.load_watchlist(con, wl_pick)) if wl_pick != "（全部股票）" else None
params = dict(
    require_above=True, require_bullish=require_bullish,
    kd_cross=kd_cross, macd_flip=macd_flip, bb_break=bb_break,
    rsi_range=list(rsi_range), bias_max=float(bias_max), rs_min=int(rs_min),
    foreign_days=foreign_days, trust_days=trust_days, total_buy=total_buy,
    fin_down_days=int(fin_down_days),
    big_increase=big_inc, retail_decrease=retail_dec,
    rev_yoy_min=float(rev_yoy_min), yield_min=float(yield_min), pe_max=float(pe_max),
    rev_high_months=int(rev_high_months), rev_yoy_pos_months=int(rev_yoy_streak),
    min_volume_lot=int(min_vol), concepts_sel=concepts_sel,
    watchlist=None if wl_pick == "（全部股票）" else wl_pick,
    watchlist_codes=wl_codes,
)
ma_periods = tuple(sorted(ma_choice)) or (5, 10, 20)
concept_map = cp.as_sets(concept_data)

with st.sidebar:  # 策略儲存按鈕要在 params 組好之後
    if new_sname and st.button(f"💾 儲存為「{new_sname}」", use_container_width=True):
        sdata = {"params": {k: v for k, v in params.items()
                            if k != "watchlist_codes"},
                 "ma_periods": list(ma_periods), "big_threshold": big_threshold}
        db.save_strategy(con, new_sname, json.dumps(sdata, ensure_ascii=False))
        st.success(f"已儲存策略「{new_sname}」")

# ══════════════════ 首次初始化 ══════════════════
if DB_EMPTY and IS_CLOUD:
    st.error("☁️ 雲端資料尚未就緒，請稍後重新整理頁面（主機每日盤後上傳資料）。")
    if st.button("重新檢查"):
        cloud.sync_db(CLOUD_URL, force=True)
        st.rerun()
    st.stop()
if DB_EMPTY:
    st.header("👋 首次使用：初始化資料")
    st.markdown(
        "資料庫是空的。初始化會下載：**股票清單**、**60 個交易日股價**、"
        "**法人/融資融券**、**月營收與估值**、**集保股權分散**。約 **8–12 分鐘**。")
    c1, c2 = st.columns(2)
    quick = c1.button("⚡ 快速初始化（約 5 分鐘，30 個交易日）", use_container_width=True)
    full = c2.button("🚀 完整初始化（約 10 分鐘，60 個交易日）", type="primary",
                     use_container_width=True)
    if quick or full:
        run_update(30 if quick else 60, 12 if quick else 15)
        st.success("初始化完成！之後可在「資料與設定」回補一年歷史以啟用回測。")
        time.sleep(1)
        st.rerun()
    st.stop()

# ══════════════════ 主頁籤 ══════════════════
tab_screen, tab_market, tab_watch, tab_bt, tab_detail, tab_settings = st.tabs(
    ["📊 篩選結果", "🌐 大盤", "⭐ 自選股", "🧪 回測", "📈 個股明細", "⚙️ 資料與設定"])

COLUMN_CONFIG = {
    "code": st.column_config.TextColumn("代號", width="small", pinned=True),
    "name": st.column_config.TextColumn("名稱", width="small", pinned=True),
    "market": st.column_config.TextColumn("市場", width="small"),
    "industry_name": st.column_config.TextColumn("產業", width="small"),
    "concepts": st.column_config.TextColumn("概念"),
    "close": st.column_config.NumberColumn("價格", format="%.2f"),
    "change_pct": st.column_config.NumberColumn("漲跌%", format="%.2f%%"),
    "volume_lot": st.column_config.NumberColumn("成交量(張)", format="%d"),
    "rs": st.column_config.NumberColumn("RS", format="%.0f"),
    "k9": st.column_config.NumberColumn("K", format="%.1f"),
    "d9": st.column_config.NumberColumn("D", format="%.1f"),
    "rsi14": st.column_config.NumberColumn("RSI", format="%.1f"),
    "macd_hist": st.column_config.NumberColumn("MACD柱", format="%.3f"),
    "bias20": st.column_config.NumberColumn("乖離20%", format="%.2f"),
    "foreign_net": st.column_config.NumberColumn("外資(張)", format="%d"),
    "foreign_streak": st.column_config.NumberColumn("外資連買", format="%d日"),
    "trust_net": st.column_config.NumberColumn("投信(張)", format="%d"),
    "trust_streak": st.column_config.NumberColumn("投信連買", format="%d日"),
    "dealer_net": st.column_config.NumberColumn("自營(張)", format="%d"),
    "total_net": st.column_config.NumberColumn("法人合計(張)", format="%d"),
    "fin_chg": st.column_config.NumberColumn("融資增減(張)", format="%d"),
    "fin_down_streak": st.column_config.NumberColumn("融資連減", format="%d日"),
    "big_pct": st.column_config.NumberColumn("大戶%", format="%.2f"),
    "big_pct_chg": st.column_config.NumberColumn("大戶週變化", format="%.2f"),
    "retail_pct": st.column_config.NumberColumn("散戶%", format="%.2f"),
    "retail_pct_chg": st.column_config.NumberColumn("散戶週變化", format="%.2f"),
    "holders": st.column_config.NumberColumn("股東人數", format="%d"),
    "rev_yoy": st.column_config.NumberColumn("營收YoY%", format="%.1f"),
    "rev_mom": st.column_config.NumberColumn("營收MoM%", format="%.1f"),
    "rev_high_streak": st.column_config.NumberColumn("營收N月新高", format="%d"),
    "rev_yoy_streak": st.column_config.NumberColumn("YoY連續月", format="%d"),
    "dyield": st.column_config.NumberColumn("殖利率%", format="%.2f"),
    "pe": st.column_config.NumberColumn("本益比", format="%.1f"),
}
SHOW_COLS = ["code", "name", "market", "industry_name", "concepts", "close",
             "change_pct", "volume_lot", "rs", "k9", "d9", "rsi14", "macd_hist",
             "bias20", "foreign_net", "foreign_streak", "trust_net", "trust_streak",
             "dealer_net", "total_net", "fin_chg", "fin_down_streak",
             "big_pct", "big_pct_chg", "retail_pct", "retail_pct_chg", "holders",
             "rev_yoy", "rev_mom", "rev_high_streak", "rev_yoy_streak", "dyield", "pe"]
for p_ in reversed(ma_periods):
    key = f"ma{p_}"
    COLUMN_CONFIG[key] = st.column_config.NumberColumn(f"MA{p_}", format="%.2f")
    SHOW_COLS.insert(7, key)

ALERT_KINDS = ["價格 ≥", "價格 ≤", "漲幅% ≥", "跌幅% ≤", "成交量(張) ≥"]


def check_alerts(quotes: dict):
    """盤中警示：每檔每日觸發一次。"""
    adf = db.list_alerts(con)
    today = date.today().isoformat()
    uni_names = dict(zip(db.universe(con)["code"], db.universe(con)["name"]))
    for _, a in adf.iterrows():
        if not a["enabled"] or a["triggered_date"] == today:
            continue
        q = quotes.get(a["code"])
        if not q or not q.get("price"):
            continue
        price, prev, vol = q["price"], q.get("prev_close"), q.get("volume") or 0
        chg = (price / prev - 1) * 100 if prev else None
        hit = ((a["kind"] == "價格 ≥" and price >= a["threshold"]) or
               (a["kind"] == "價格 ≤" and price <= a["threshold"]) or
               (a["kind"] == "漲幅% ≥" and chg is not None and chg >= a["threshold"]) or
               (a["kind"] == "跌幅% ≤" and chg is not None and chg <= a["threshold"]) or
               (a["kind"] == "成交量(張) ≥" and vol >= a["threshold"]))
        if hit:
            name = uni_names.get(a["code"], "")
            msg = (f"{a['code']} {name} 現價 {price}"
                   + (f"（{chg:+.1f}%）" if chg is not None else "")
                   + f" 觸發「{a['kind']} {a['threshold']:g}」")
            notify.windows_toast("🚨 盤中警示", msg)
            st.toast(msg, icon="🚨")
            db.mark_alert_triggered(con, int(a["id"]), today)


def get_filtered_table(live=None):
    if live is not None:
        table = screener.build_table(db.connect(), ma_periods, live, big_threshold,
                                     prices_df=cached_recent_prices(data_version()))
    else:
        table = cached_table(ma_periods, big_threshold, data_version())
    if table.empty:
        return table, table
    filtered = screener.apply_filters(table, params, concept_map)
    filtered = screener.tag_concepts(filtered, concept_map)
    filtered = filtered.sort_values("total_net", ascending=False, na_position="last")
    return table, filtered


@st.fragment(run_every=f"{refresh_min}m" if live_mode else None)
def screen_view():
    live = None
    if live_mode:
        tick = int(time.time() // (refresh_min * 60))
        with st.spinner("抓取盤中即時報價…"):
            if full_poll:
                live = cached_live_quotes(tick, None)
            else:
                base = cached_table(ma_periods, big_threshold, data_version())
                near = pd.Series(True, index=base.index)
                for p_ in ma_periods:
                    near &= (base["close"] / base[f"ma{p_}"] - 1).abs() <= 0.08
                cand = set(base.loc[near.fillna(False), "code"])
                cand |= set(db.list_alerts(con)["code"])
                for wname in db.watchlist_names(con):
                    cand |= set(db.load_watchlist(con, wname))
                live = cached_live_quotes(tick, tuple(sorted(cand)))
        check_alerts(live)
        st.caption(f"🔴 盤中即時 — {datetime.now():%H:%M:%S} 更新"
                   f"（{'全市場' if full_poll else f'候選 {len(live)} 檔'}），"
                   f"每 {refresh_min} 分鐘刷新；籌碼與基本面為盤後資料")
    table, filtered = get_filtered_table(live)
    if table.empty:
        st.warning("尚無足夠股價資料，請先到「資料與設定」執行更新。")
        return

    left, right = st.columns([3, 1])
    tdcc_s = table["tdcc_date"].dropna()
    left.markdown(
        f"**{len(filtered)}** 檔通過篩選（全市場 {len(table)} 檔，"
        f"股價：{table['last_date'].iloc[0]}，"
        f"集保週：{tdcc_s.iloc[0] if len(tdcc_s) else '—'}）")
    right.download_button("⬇️ 下載 CSV",
                          filtered[SHOW_COLS].to_csv(index=False).encode("utf-8-sig"),
                          file_name=f"screen_{datetime.now():%Y%m%d_%H%M}.csv",
                          use_container_width=True)

    if show_badge:
        pj = json.dumps({**{k: v for k, v in params.items()
                            if k != "watchlist_codes"},
                         "watchlist_codes": sorted(wl_codes) if wl_codes else None},
                        sort_keys=True, ensure_ascii=False)
        stats = cached_quickstats(pj, ma_periods, data_version())
        if stats.get("error"):
            st.caption(f"📜 歷史勝率標註不可用：{stats['error']}")
        else:
            s20 = stats["horizons"].get(20, {})
            if s20.get("n"):
                skip_note = ("（未計入：" + "、".join(stats["skipped"]) + "）"
                             if stats["skipped"] else "")
                st.info(f"📜 **此條件近 {stats['n_days']} 個交易日**：觸發 "
                        f"{s20['n']:,} 次｜20日勝率 **{s20['win_rate']:.1f}%**"
                        f"（全市場 {s20['bench_win']:.1f}%）｜平均報酬 "
                        f"**{s20['mean']:+.2f}%**（全市場 {s20['bench_mean']:+.2f}%）"
                        f"{skip_note} — 詳見「回測」頁")
            else:
                st.caption("📜 此條件在近一年沒有任何歷史觸發")

    st.dataframe(filtered[SHOW_COLS], column_config=COLUMN_CONFIG,
                 use_container_width=True, height=500, hide_index=True)

    c1, c2 = st.columns(2)
    with c1.expander("⭐ 把目前結果存成自選股清單"):
        wl_new = st.text_input("清單名稱", value=f"篩選{datetime.now():%m%d}")
        if st.button("儲存清單") and wl_new:
            db.save_watchlist(db.connect(), wl_new, filtered["code"].tolist())
            st.success(f"已存 {len(filtered)} 檔到「{wl_new}」")
    with c2.expander("📋 概念股分布統計"):
        rows = []
        for tag in concept_options:
            sub = screener.apply_filters(
                filtered, {"require_above": False, "concepts_sel": [tag]}, concept_map)
            rows.append({"概念": tag, "通過篩選檔數": len(sub)})
        st.dataframe(pd.DataFrame(rows), hide_index=True)


with tab_screen:
    screen_view()

# ══════════════════ 大盤 ══════════════════
with tab_market:
    table = cached_table(ma_periods, big_threshold, data_version())
    if table.empty:
        st.info("請先更新資料")
    else:
        up = int((table["change_pct"] > 0).sum())
        down = int((table["change_pct"] < 0).sum())
        flat = len(table) - up - down
        above20 = table["close"] > table.get("ma20", pd.Series(dtype=float))
        breadth_today = above20.mean() * 100 if "ma20" in table else None
        idx = db.load_index(con, 90)
        m1, m2, m3, m4 = st.columns(4)
        if not idx.empty:
            chg = (idx["taiex"].iloc[-1] / idx["taiex"].iloc[-2] - 1) * 100 \
                if len(idx) >= 2 else 0
            m1.metric("加權指數", f"{idx['taiex'].iloc[-1]:,.0f}", f"{chg:+.2f}%")
        m2.metric("上漲 / 下跌 / 平盤", f"{up} / {down} / {flat}")
        if breadth_today is not None:
            m3.metric("站上 20MA 比例（市場寬度）", f"{breadth_today:.1f}%")
        insti30 = db.load_insti(con, 1)
        if not insti30.empty:
            total_net = insti30["total_net"].sum() / 1000
            m4.metric("三大法人合計買賣超", f"{total_net:+,.0f} 張")

        cA, cB = st.columns(2)
        with cA:
            # 市場寬度歷史（站上20MA比例）
            prices90 = cached_recent_prices(data_version())
            from src import indicators as ind_mod
            pv = ind_mod.make_pivots(prices90)
            cpv = pv["close"]
            breadth = (cpv > cpv.rolling(20).mean()).sum(axis=1) / \
                cpv.notna().sum(axis=1) * 100
            breadth = breadth.iloc[20:]
            figb = go.Figure(go.Scatter(x=breadth.index, y=breadth.values,
                                        fill="tozeroy", line_color="#5b8def"))
            figb.add_hline(y=50, line_dash="dash", line_color="#888")
            figb.update_layout(title="市場寬度：站上 20MA 的股票比例（%）",
                               height=300, margin=dict(l=10, r=10, t=40, b=10))
            figb.update_xaxes(type="category", nticks=8)
            st.plotly_chart(figb, use_container_width=True)
        with cB:
            if not idx.empty:
                figi = go.Figure(go.Scatter(x=idx["date"], y=idx["taiex"],
                                            line_color="#d62728"))
                figi.update_layout(title="加權指數（90 日）", height=300,
                                   margin=dict(l=10, r=10, t=40, b=10))
                figi.update_xaxes(type="category", nticks=8)
                st.plotly_chart(figi, use_container_width=True)

        cC, cD = st.columns(2)
        with cC:
            insti_hist = db.load_insti(con, 30)
            if not insti_hist.empty:
                g = insti_hist.groupby("date")[["foreign_net", "trust_net",
                                                "dealer_net"]].sum() / 1000
                figf = go.Figure()
                for col, nm, color in (("foreign_net", "外資", "#1f77b4"),
                                       ("trust_net", "投信", "#ff7f0e"),
                                       ("dealer_net", "自營", "#7f7f7f")):
                    figf.add_trace(go.Bar(x=g.index, y=g[col], name=nm,
                                          marker_color=color))
                figf.update_layout(barmode="relative", height=300,
                                   title="三大法人全市場買賣超（張，30 日）",
                                   margin=dict(l=10, r=10, t=40, b=10),
                                   legend=dict(orientation="h", y=1.12))
                figf.update_xaxes(type="category", nticks=8)
                st.plotly_chart(figf, use_container_width=True)
        with cD:
            ind_avg = table.groupby("industry_name")["change_pct"].mean() \
                .sort_values()
            colors = ["#2ca02c" if v < 0 else "#d62728" for v in ind_avg.values]
            figh = go.Figure(go.Bar(x=ind_avg.values, y=ind_avg.index,
                                    orientation="h", marker_color=colors))
            figh.update_layout(title="產業平均漲跌%（今日）",
                               height=max(300, 16 * len(ind_avg)),
                               margin=dict(l=10, r=10, t=40, b=10))
            st.plotly_chart(figh, use_container_width=True)

# ══════════════════ 自選股＋警示 ══════════════════
with tab_watch:
    names = db.watchlist_names(con)
    uni = db.universe(con)
    name_map = dict(zip(uni["code"], uni["name"]))
    cnew1, cnew2 = st.columns([3, 1])
    new_name = cnew1.text_input("新增清單名稱", key="wl_create_name")
    if cnew2.button("建立空白清單", use_container_width=True) and new_name:
        db.save_watchlist(con, new_name, [])
        st.rerun()
    if not names:
        st.info("還沒有自選股清單。可在上方建立，或在「篩選結果」頁把結果一鍵存成清單。")
    for nm in names:
        codes = db.load_watchlist(con, nm)
        with st.expander(f"⭐ {nm}（{len(codes)} 檔）", expanded=len(names) == 1):
            txt = st.text_area("股票代號（空白/逗號/換行分隔）", " ".join(codes),
                               key=f"wl_{nm}", height=80)
            edited = [c for c in txt.replace(",", " ").replace("、", " ").split() if c]
            valid = [c for c in edited if c in name_map]
            b1, b2, _ = st.columns([1, 1, 3])
            if b1.button("儲存", key=f"wl_save_{nm}"):
                db.save_watchlist(con, nm, valid)
                st.rerun()
            if b2.button("刪除清單", key=f"wl_del_{nm}"):
                db.delete_watchlist(con, nm)
                st.rerun()
            if codes:
                tbl = cached_table(ma_periods, big_threshold, data_version())
                if not tbl.empty:
                    sub = screener.tag_concepts(tbl[tbl["code"].isin(codes)],
                                                concept_map)
                    st.dataframe(sub[SHOW_COLS], column_config=COLUMN_CONFIG,
                                 use_container_width=True, hide_index=True,
                                 height=min(420, 40 + 36 * len(sub)))

    st.divider()
    st.subheader("🚨 盤中警示")
    st.caption("開啟側邊欄「盤中即時價」時自動檢查，觸發即發 Windows 通知"
               "（每檔每日一次）。跌幅門檻請填負數，例如 -3。")
    adf = db.list_alerts(con)
    if adf.empty:
        adf = pd.DataFrame(columns=["id", "code", "kind", "threshold", "enabled",
                                    "triggered_date"])
    edited = st.data_editor(
        adf[["code", "kind", "threshold", "enabled"]],
        column_config={
            "code": st.column_config.TextColumn("代號", required=True),
            "kind": st.column_config.SelectboxColumn("條件", options=ALERT_KINDS,
                                                     required=True),
            "threshold": st.column_config.NumberColumn("門檻", required=True),
            "enabled": st.column_config.CheckboxColumn("啟用", default=True),
        },
        num_rows="dynamic", hide_index=True, use_container_width=True)
    if st.button("儲存警示設定"):
        db.replace_alerts(con, edited)
        st.success("已儲存")

# ══════════════════ 回測 ══════════════════
with tab_bt:
    st.markdown("**用歷史資料驗證目前側邊欄的篩選條件**。基準為**全市場等權平均**"
                "（所有普通股同期平均報酬，非加權指數）。")
    n_price_days = len(db.price_dates(con))
    st.caption(f"資料庫現有 {n_price_days} 個交易日股價、"
               f"{len(db.insti_dates(con))} 日法人、{len(db.margin_dates(con))} 日融資融券。"
               + ("建議先到「資料與設定」回補一年歷史。" if n_price_days < 150 else ""))

    st.subheader("① 條件勝率統計（重疊持有，快速驗證）")
    if st.button("▶️ 執行勝率統計", type="primary"):
        with st.spinner("計算中…"):
            res = bt.run(db.connect(), params, ma_periods, concept_map=concept_map)
        st.session_state["bt_result"] = res
    res = st.session_state.get("bt_result")
    if res:
        if res.get("error"):
            st.error(res["error"])
        else:
            if res["skipped"]:
                st.warning("因歷史深度不足未納入的條件：" + "、".join(res["skipped"]))
            st.caption(f"回測期間：{res['date_range'][0]} ～ {res['date_range'][1]}"
                       f"（{res['n_days']} 個交易日）")
            for h, s in res["horizons"].items():
                if s.get("n", 0) == 0:
                    st.info(f"{h} 日後：期間內沒有任何觸發")
                    continue
                m1, m2, m3, m4, m5 = st.columns(5)
                m1.metric(f"{h}日後 觸發次數", f"{s['n']:,}")
                m2.metric("勝率", f"{s['win_rate']:.1f}%",
                          f"{s['win_rate'] - s['bench_win']:+.1f}% vs 全市場")
                m3.metric("平均報酬", f"{s['mean']:+.2f}%",
                          f"{s['mean'] - s['bench_mean']:+.2f}% vs 全市場")
                m4.metric("中位數報酬", f"{s['median']:+.2f}%")
                m5.metric("全市場同期平均", f"{s['bench_mean']:+.2f}%")
            if "returns_sample" in res:
                hmax = max(res["horizons"].keys())
                fig = go.Figure(go.Histogram(x=res["returns_sample"], nbinsx=60,
                                             marker_color="#5b8def"))
                fig.add_vline(x=0, line_color="#888", line_dash="dash")
                fig.update_layout(title=f"{hmax} 日後報酬分布（%）", height=300,
                                  margin=dict(l=10, r=10, t=40, b=10))
                st.plotly_chart(fig, use_container_width=True)

    st.divider()
    st.subheader("② 完整策略回測（非重疊持有、含成本與停損停利）")
    b1, b2, b3, b4, b5, b6 = st.columns(6)
    entry_sel = b1.selectbox("進場價", ["次日開盤", "觸發日收盤"])
    hold_n = b2.slider("最長持有（交易日）", 1, 60, 20)
    sl = b3.number_input("停損 %（0=不設）", 0.0, 30.0, 7.0, step=0.5)
    tp = b4.number_input("停利 %（0=不設）", 0.0, 100.0, 0.0, step=1.0)
    use_cost = b5.checkbox("計入交易成本 0.585%", value=True)
    ex_limit = b6.checkbox("排除觸發日漲停", value=True,
                           help="觸發當天收漲停的股票隔日多半買不到，剔除以貼近實戰")
    if st.button("▶️ 執行完整回測", type="primary", key="run_bt2"):
        with st.spinner("逐筆交易模擬中…（約 10–30 秒）"):
            res2 = bt.run_strategy(
                db.connect(), params, ma_periods, concept_map=concept_map,
                entry="next_open" if entry_sel == "次日開盤" else "close",
                hold_days=hold_n, stop_loss=sl, take_profit=tp,
                cost=use_cost, exclude_limit_up=ex_limit)
        st.session_state["bt2_result"] = res2
    res2 = st.session_state.get("bt2_result")
    if res2:
        if res2.get("error"):
            st.error(res2["error"])
        else:
            s = res2["stats"]
            if res2["skipped"]:
                st.warning("未納入條件：" + "、".join(res2["skipped"]))
            n1, n2, n3, n4, n5, n6 = st.columns(6)
            n1.metric("交易筆數", f"{s['n']:,}")
            n2.metric("勝率", f"{s['win_rate']:.1f}%")
            n3.metric("平均/中位報酬", f"{s['mean']:+.2f}% / {s['median']:+.2f}%")
            pf = s["profit_factor"]
            n4.metric("獲利因子", "∞" if pf == float("inf") else f"{pf:.2f}")
            n5.metric("策略累積報酬", f"{s['total_return']:+.1f}%",
                      f"{s['total_return'] - s['bench_return']:+.1f}% vs 全市場")
            n6.metric("最大回檔", f"{s['max_drawdown']:.1f}%")
            st.caption(f"平均持有 {s['avg_days']:.1f} 日｜出場原因：" +
                       "、".join(f"{k} {v} 筆" for k, v in s["reasons"].items()) +
                       f"｜期間 {res2['date_range'][0]} ～ {res2['date_range'][1]}"
                       "（資金曲線＝每日在倉交易的等權平均報酬複利）")
            fige = go.Figure()
            fige.add_trace(go.Scatter(x=res2["equity"].index,
                                      y=res2["equity"].values, name="策略",
                                      line=dict(color="#d62728", width=2)))
            fige.add_trace(go.Scatter(x=res2["bench"].index,
                                      y=res2["bench"].values, name="全市場等權",
                                      line=dict(color="#7f7f7f", width=1.4)))
            fige.update_layout(title="資金曲線", height=340,
                               margin=dict(l=10, r=10, t=40, b=10),
                               legend=dict(orientation="h", y=1.1))
            fige.update_xaxes(type="category", nticks=10)
            st.plotly_chart(fige, use_container_width=True)
            with st.expander(f"交易明細（{len(res2['trades'])} 筆）"):
                st.download_button("⬇️ 下載交易明細 CSV",
                                   res2["trades"].to_csv(index=False)
                                   .encode("utf-8-sig"),
                                   file_name="trades.csv")
                st.dataframe(res2["trades"].tail(200), hide_index=True,
                             use_container_width=True)
    st.caption("⚠️ 回測僅供條件有效性參考，過去績效不代表未來，不構成投資建議。")

# ══════════════════ 個股明細 ══════════════════
with tab_detail:
    uni = db.universe(con)
    uni["label"] = uni["code"] + " " + uni["name"]
    sel = st.selectbox("選擇個股", uni["label"].tolist(), key="detail_stock")
    code = sel.split(" ")[0]

    hist = db.load_stock_history(con, code, 160)
    if hist.empty:
        st.info("此股票尚無歷史資料。")
    else:
        info = uni[uni["code"] == code].iloc[0]
        st.subheader(f"{code} {info['name']}（{info['market']}／{info['industry_name']}）")

        h_ind = hist.set_index("date")
        c_ = h_ind["close"]
        low9 = h_ind["low"].rolling(9).min()
        high9 = h_ind["high"].rolling(9).max()
        span = high9 - low9
        rsv = ((c_ - low9) / span.where(span > 0) * 100).clip(0, 100).fillna(50)
        k_ = rsv.ewm(alpha=1 / 3, adjust=False).mean()
        d_ = k_.ewm(alpha=1 / 3, adjust=False).mean()
        macd_ = c_.ewm(span=12, adjust=False).mean() - c_.ewm(span=26, adjust=False).mean()
        sig_ = macd_.ewm(span=9, adjust=False).mean()

        fig = make_subplots(rows=4, cols=1, shared_xaxes=True,
                            row_heights=[0.5, 0.16, 0.17, 0.17],
                            vertical_spacing=0.02)
        fig.add_trace(go.Candlestick(
            x=hist["date"], open=hist["open"], high=hist["high"],
            low=hist["low"], close=hist["close"], name="K線",
            increasing_line_color="#d62728", decreasing_line_color="#2ca02c"), 1, 1)
        for p_, color in zip(ma_periods, ("#1f77b4", "#ff7f0e", "#9467bd", "#8c564b")):
            fig.add_trace(go.Scatter(x=hist["date"],
                                     y=hist["close"].rolling(p_).mean(),
                                     name=f"MA{p_}", line=dict(width=1.4, color=color)), 1, 1)
        fig.add_trace(go.Bar(x=hist["date"], y=hist["volume"] / 1000, name="成交量(張)",
                             marker_color="#7f7f7f"), 2, 1)
        fig.add_trace(go.Scatter(x=hist["date"], y=k_.values, name="K",
                                 line=dict(width=1.2, color="#1f77b4")), 3, 1)
        fig.add_trace(go.Scatter(x=hist["date"], y=d_.values, name="D",
                                 line=dict(width=1.2, color="#ff7f0e")), 3, 1)
        fig.add_trace(go.Bar(x=hist["date"], y=(macd_ - sig_).values, name="MACD柱",
                             marker_color="#9467bd"), 4, 1)
        fig.add_trace(go.Scatter(x=hist["date"], y=macd_.values, name="DIF",
                                 line=dict(width=1, color="#d62728")), 4, 1)
        fig.add_trace(go.Scatter(x=hist["date"], y=sig_.values, name="MACD",
                                 line=dict(width=1, color="#2ca02c")), 4, 1)
        fig.update_layout(height=680, xaxis_rangeslider_visible=False,
                          margin=dict(l=10, r=10, t=10, b=10),
                          legend=dict(orientation="h", y=1.04))
        fig.update_xaxes(type="category", nticks=12)
        st.plotly_chart(fig, use_container_width=True)

        c1, c2, c3 = st.columns(3)
        with c1:
            st.markdown("**三大法人買賣超（張）**")
            si = db.load_stock_insti(con, code, 30)
            if si.empty:
                st.caption("無法人資料")
            else:
                fig2 = go.Figure()
                for col, nm_, color in (("foreign_net", "外資", "#1f77b4"),
                                        ("trust_net", "投信", "#ff7f0e"),
                                        ("dealer_net", "自營", "#7f7f7f")):
                    fig2.add_trace(go.Bar(x=si["date"], y=si[col] / 1000, name=nm_,
                                          marker_color=color))
                fig2.update_layout(barmode="relative", height=280,
                                   margin=dict(l=10, r=10, t=10, b=10),
                                   legend=dict(orientation="h", y=1.15))
                fig2.update_xaxes(type="category", nticks=6)
                st.plotly_chart(fig2, use_container_width=True)
            st.markdown("**融資餘額（張）**")
            sm = db.load_stock_margin(con, code, 30)
            if sm.empty:
                st.caption("無融資融券資料")
            else:
                fig5 = go.Figure(go.Scatter(x=sm["date"], y=sm["fin_balance"],
                                            line_color="#9467bd"))
                fig5.update_layout(height=200, margin=dict(l=10, r=10, t=10, b=10))
                fig5.update_xaxes(type="category", nticks=6)
                st.plotly_chart(fig5, use_container_width=True)
        with c2:
            st.markdown(f"**大戶（≥{big_threshold}張）/ 散戶持股比例**")
            std = db.load_stock_tdcc(con, code)
            if std.empty:
                st.caption("無集保資料")
            else:
                from src.sources.tdcc import RETAIL_MAX_LEVEL, THRESHOLD_LEVEL
                min_level = THRESHOLD_LEVEL.get(big_threshold, 12)
                big_s = std[(std["level"] >= min_level) & (std["level"] <= 15)] \
                    .groupby("date")["pct"].sum()
                retail_s = std[std["level"] <= RETAIL_MAX_LEVEL] \
                    .groupby("date")["pct"].sum()
                fig3 = go.Figure()
                fig3.add_trace(go.Scatter(x=big_s.index, y=big_s.values, name="大戶%",
                                          line=dict(color="#d62728")))
                fig3.add_trace(go.Scatter(x=retail_s.index, y=retail_s.values,
                                          name="散戶%", line=dict(color="#2ca02c")))
                fig3.update_layout(height=280, margin=dict(l=10, r=10, t=10, b=10),
                                   legend=dict(orientation="h", y=1.15))
                st.plotly_chart(fig3, use_container_width=True)
        with c3:
            st.markdown("**月營收（億元）與 YoY%**")
            rv = db.load_stock_revenue(con, code, 24)
            if rv.empty:
                st.caption("無營收資料")
            else:
                fig4 = make_subplots(specs=[[{"secondary_y": True}]])
                fig4.add_trace(go.Bar(x=rv["month"], y=rv["revenue"] / 100000,
                                      name="月營收(億)", marker_color="#8aa9b8"))
                fig4.add_trace(go.Scatter(x=rv["month"], y=rv["yoy"], name="YoY%",
                                          line=dict(color="#d62728")),
                               secondary_y=True)
                fig4.update_layout(height=280, margin=dict(l=10, r=10, t=10, b=10),
                                   legend=dict(orientation="h", y=1.15))
                st.plotly_chart(fig4, use_container_width=True)

# ══════════════════ 資料與設定 ══════════════════
with tab_settings:
    if IS_CLOUD:
        st.subheader("☁️ 雲端版資料同步")
        st.markdown(f"**{sync_status}**｜資料由主機每日盤後更新並上傳，"
                    "此處只需等待自動同步。")
        if st.button("🔄 立即檢查是否有新資料"):
            msg = cloud.sync_db(CLOUD_URL, force=True)
            st.cache_data.clear()
            st.success(msg)
            st.rerun()
        st.caption("雲端版限制：無盤中即時報價、無推播（請在主機端設定推播）；"
                   "概念股清單修改在雲端重啟後會還原，請於主機端維護。")
        st.divider()
        st.subheader("🩺 資料來源健康狀態（主機端更新時記錄）")
        h = db.health(con)
        if not h.empty:
            st.dataframe(h, hide_index=True, use_container_width=True)
        st.stop()

    st.subheader("🔄 資料更新")
    c1, c2, c3, c4 = st.columns(4)
    if c1.button("每日更新（增量）", type="primary", use_container_width=True):
        r = run_update(10, 10)
        st.success(f"完成：{r}")
        st.rerun()
    if c2.button("完整回補（60 交易日）", use_container_width=True):
        r = run_update(60, 15)
        st.success(f"完成：{r}")
        st.rerun()
    if c3.button("回補一年歷史（背景執行）", use_container_width=True,
                 help="回測所需，約 40–50 分鐘，背景執行期間可正常使用"):
        subprocess.Popen(
            [sys.executable, "-X", "utf8",
             str(db.ROOT / "scripts" / "daily_update.py"), "--year"],
            creationflags=0x08000008, cwd=str(db.ROOT))
        st.success("已在背景開始回補，進度見 data/update.log。")
    if c4.button("回補營收歷史（24 個月）", use_container_width=True,
                 help="啟用「營收創N月新高」「YoY連續成長」篩選，約 2–3 分鐘"):
        bar = st.progress(0.0)
        n = updater.backfill_revenue_history(
            db.connect(), 24,
            lambda m, f=None: bar.progress(min(f or 0, 1.0), text=m))
        st.cache_data.clear()
        st.success(f"完成：{n} 筆")

    st.divider()
    st.subheader("✅ 一鍵資料健檢")
    st.caption("完整性掃描＋與獨立官方端點抽樣比對＋指標演算對照，約 20 秒。")
    if st.button("🩺 執行健檢"):
        with st.spinner("健檢中…"):
            results = healthcheck.run_all(db.connect())
        st.session_state["hc"] = results
    if st.session_state.get("hc"):
        hc_df = pd.DataFrame(st.session_state["hc"])
        n_bad = (hc_df["狀態"] == "❌").sum()
        (st.success if n_bad == 0 else st.error)(
            f"健檢完成：{len(hc_df)} 項，異常 {n_bad} 項")
        st.dataframe(hc_df, hide_index=True, use_container_width=True)

    st.divider()
    st.subheader("🔔 盤後推播通知")
    cfg = notify.load_push_config(con)
    pc1, pc2 = st.columns(2)
    with pc1:
        win_on = st.toggle("Windows 桌面通知", value=cfg.get("windows", False))
        snames_all = db.strategy_names(con)
        push_strats = st.multiselect(
            "推播策略（可多選；留空則用下方按鈕存的單一條件）",
            snames_all, default=[s for s in cfg.get("strategies", [])
                                 if s in snames_all])
        if st.button("🔔 發送測試通知（Windows）"):
            ok, err = notify.windows_toast("台股篩選器測試", "推播管道正常 ✅")
            st.success("已送出") if ok else st.error(f"失敗：{err}")
    with pc2:
        line_on = st.toggle("LINE 推播", value=cfg.get("line", False))
        line_token = st.text_input("LINE Channel Access Token",
                                   value=db.get_meta(con, "line_token") or "",
                                   type="password")
        line_uid = st.text_input("LINE 收件人（填 all＝廣播給所有加入官方帳號的好友；"
                                 "或填 U 開頭 ID，逗號分隔可多人）",
                                 value=db.get_meta(con, "line_user_id") or "all")
        st.caption("免費額度 200 則/月「按收件人數計」：廣播給 4 人＝每次扣 4 則。"
                   "家人只要掃官方帳號 QR 加好友即可收到。")
        if st.button("🔔 發送測試訊息（LINE）"):
            if line_token and line_uid:
                ok, err = notify.line_push(line_token, line_uid, "台股篩選器測試 ✅")
                st.success("已送出") if ok else st.error(f"失敗：{err}")
            else:
                st.warning("請先填 token 與 user ID")
    if st.button("💾 儲存推播設定（含目前側邊欄條件作為備用）", type="primary"):
        db.set_meta(con, "line_token", line_token)
        db.set_meta(con, "line_user_id", line_uid)
        notify.save_push_config(con, {
            "windows": win_on, "line": line_on,
            "strategies": push_strats,
            "params": {k: v for k, v in params.items() if k != "watchlist_codes"},
            "ma_periods": list(ma_periods), "big_threshold": big_threshold,
            "top_n": 15})
        st.success("已儲存。每日排程更新後自動推播。")
    if st.button("▶️ 立即執行推播（測試整條流程）"):
        for line in notify.push_screen_results(db.connect()):
            st.write("•", line)

    st.divider()
    st.subheader("📟 盤中 K 棒監控（1分/5分連紅通知）")
    st.caption("獨立監控程式在盤中每 20 秒取樣重建 1分/5分 K 棒，偵測連續收紅後"
               "推播 LINE／Windows 通知。監控對象取自自選股清單。"
               "啟動方式：雙擊「啟動盤中監控.bat」，或雙擊「設定盤中監控排程.bat」"
               "讓它每個平日 08:58 自動啟動（13:32 自動收工）。")
    icfg_data = intraday.load_config()
    ic1, ic2, ic3, ic4 = st.columns(4)
    i_enabled = ic1.toggle("啟用監控", value=icfg_data.get("enabled", False))
    wl_all = db.watchlist_names(con)
    i_wl = ic2.selectbox("監控清單", ["（所有自選股清單）"] + wl_all,
                         index=(wl_all.index(icfg_data["watchlist"]) + 1
                                if icfg_data.get("watchlist") in wl_all else 0))
    i_k1 = ic3.slider("1分K 連續收紅根數", 1, 10, icfg_data.get("k1_count", 3))
    i_k5 = ic4.slider("5分K 連續收紅根數", 0, 6, icfg_data.get("k5_count", 2),
                      help="0 = 不看 5 分K")
    ic5, ic6, ic7, ic8 = st.columns(4)
    i_mode = ic5.selectbox("條件組合", ["AND（同時成立）", "OR（任一成立）"],
                           index=0 if icfg_data.get("mode", "AND") == "AND" else 1)
    i_cool = ic6.slider("同檔冷卻（分鐘）", 5, 240, icfg_data.get("cooldown_min", 30))
    i_line = ic7.checkbox("LINE 通知", value=icfg_data.get("line", True),
                          help="使用上方推播區塊設定的 LINE token/user ID")
    i_win = ic8.checkbox("Windows 通知", value=icfg_data.get("windows", True))
    if st.button("💾 儲存監控設定"):
        intraday.save_config({"enabled": i_enabled,
                          "watchlist": "" if i_wl == "（所有自選股清單）" else i_wl,
                          "k1_count": int(i_k1), "k5_count": int(i_k5),
                          "mode": "AND" if i_mode.startswith("AND") else "OR",
                          "line": i_line, "windows": i_win,
                          "cooldown_min": int(i_cool), "poll_sec": 20})
        st.success("已儲存。明天開盤起生效（記得設定排程或手動啟動監控程式）。")

    st.divider()
    st.subheader("☁️ 雲端發佈（給家人朋友的手機版）")
    st.caption("每日更新後自動把資料上傳到你的 GitHub，Streamlit Cloud 上的網頁"
               "會自動同步——設定方式見專案資料夾的《手機部署指南.md》。")
    ccfg = cloud.load_publish_config()
    gc1, gc2 = st.columns(2)
    gh_repo = gc1.text_input("GitHub 儲存庫（帳號/名稱）",
                             value=ccfg.get("repo", ""),
                             placeholder="例如 wilson/tw-stock-screener")
    gh_token = gc2.text_input("GitHub Token（需 repo 權限）",
                              value=ccfg.get("token", ""), type="password")
    bc1, bc2 = st.columns(2)
    if bc1.button("💾 儲存雲端發佈設定", use_container_width=True):
        cloud.save_publish_config(gh_repo, gh_token)
        st.success("已儲存，之後每日排程更新後會自動發佈")
    if bc2.button("🚀 立即發佈一次", use_container_width=True,
                  disabled=not (gh_repo and gh_token)):
        cloud.save_publish_config(gh_repo, gh_token)
        with st.spinner("上傳中…（約 1–5 分鐘，視上傳頻寬）"):
            msg = cloud.publish()
        st.success(msg) if msg.startswith("✅") else st.error(msg)

    st.divider()
    st.subheader("🩺 資料來源健康狀態")
    h = db.health(con)
    if h.empty:
        st.caption("尚無紀錄")
    else:
        st.dataframe(h, hide_index=True, use_container_width=True)

    st.divider()
    st.subheader("🧩 集保大戶歷史回補（FinMind 備援）")
    st.caption("**需要 FinMind API token（免費）**："
               "至 [finmindtrade.com](https://finmindtrade.com) 註冊後貼到下方。")
    token = st.text_input("FinMind API Token",
                          value=db.get_meta(con, "finmind_token") or "",
                          type="password")
    if token != (db.get_meta(con, "finmind_token") or ""):
        db.set_meta(con, "finmind_token", token)
        st.toast("Token 已儲存")

    def _run_tdcc_backfill(codes):
        bar = st.progress(0.0)
        done, err = updater.backfill_tdcc_history(
            db.connect(), codes, 8,
            lambda m, f=None: bar.progress(min(f or 0, 1.0), text=m))
        st.cache_data.clear()
        if err:
            st.error(f"完成 {done}/{len(codes)} 檔；{err}")
        else:
            st.success(f"完成 {done}/{len(codes)} 檔")

    cc1, cc2 = st.columns(2)
    if cc1.button("回補概念股清單的集保歷史（8 週）", use_container_width=True):
        _run_tdcc_backfill(sorted(set().union(*cp.as_sets().values())))
    if cc2.button("回補目前篩選結果的集保歷史（8 週）", use_container_width=True):
        _, filtered = get_filtered_table()
        _run_tdcc_backfill(filtered["code"].tolist()[:150])

    st.divider()
    st.subheader("🏷️ 概念股清單管理")
    data = cp.load()
    uni_codes = set(db.universe(con)["code"])
    name_map = dict(zip(db.universe(con)["code"], db.universe(con)["name"]))
    for tag in list(data.keys()):
        with st.expander(f"{tag}（{len(data[tag])} 檔）"):
            txt = st.text_area("股票代號（空白/逗號/換行分隔）",
                               " ".join(data[tag]), key=f"ta_{tag}", height=100)
            codes = [c for c in txt.replace(",", " ").replace("、", " ").split() if c]
            valid = [c for c in codes if c in uni_codes]
            invalid = [c for c in codes if c not in uni_codes]
            if invalid:
                st.warning(f"不在上市/上櫃清單中（將忽略）：{', '.join(invalid)}")
            st.caption("、".join(f"{c} {name_map.get(c, '?')}" for c in valid[:80]))
            if st.button("儲存", key=f"save_{tag}"):
                data[tag] = valid
                cp.save(data)
                st.cache_data.clear()
                st.rerun()

    with st.expander("➕ 新增概念 / 🌐 從網頁匯入"):
        new_tag = st.text_input("新概念名稱（例如：低軌衛星）")
        if st.button("建立空白概念") and new_tag and new_tag not in data:
            data[new_tag] = []
            cp.save(data)
            st.rerun()
        st.markdown("---")
        url = st.text_input("網頁網址（貼上列出概念股的頁面，自動抓出有效股號）")
        target = st.selectbox("匯入到概念", list(data.keys()) or ["（先建立概念）"])
        if st.button("抓取預覽") and url and data:
            try:
                found = cp.extract_codes_from_url(url, uni_codes)
                st.session_state["import_preview"] = found
                st.session_state["import_target"] = target
            except Exception as e:
                st.error(f"抓取失敗：{e}")
        if st.session_state.get("import_preview"):
            found = st.session_state["import_preview"]
            st.write(f"找到 {len(found)} 檔：",
                     "、".join(f"{c} {name_map.get(c, '?')}" for c in found[:100]))
            if st.button(f"確認合併到「{st.session_state['import_target']}」"):
                t_ = st.session_state["import_target"]
                data[t_] = sorted(set(data[t_]) | set(found))
                cp.save(data)
                st.session_state.pop("import_preview")
                st.cache_data.clear()
                st.rerun()
