"""技術指標引擎：以 (日期 × 股票) 矩陣向量化計算，篩選與回測共用。

所有函式接受/回傳 pandas DataFrame（index=日期字串、columns=股票代號），
一次算完全市場，避免逐檔迴圈。
"""
import pandas as pd


def make_pivots(prices: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """prices: db.load_prices 的長表 → {'open','high','low','close','volume'} 寬表。"""
    out = {}
    for col in ("open", "high", "low", "close", "volume"):
        out[col] = prices.pivot_table(index="date", columns="code", values=col,
                                      aggfunc="last").sort_index()
    return out


def _gap_columns(df: pd.DataFrame) -> list:
    """找出「上市期間內有停牌缺口」的股票（通常只有零星幾檔）。"""
    v = df.notna()
    seen = v.cummax()
    seen_rev = v.iloc[::-1].cummax().iloc[::-1]
    interior_na = (~v) & seen & seen_rev
    return list(df.columns[interior_na.any()])


def _rolling_gapless(df: pd.DataFrame, window: int, fn: str) -> pd.DataFrame:
    """rolling 窗以「實際交易日」計算（同看盤軟體慣例）。
    無缺口的股票走快速矩陣運算，只有缺口股逐欄壓縮重算。"""
    out = getattr(df.rolling(window, min_periods=window), fn)()
    for col in _gap_columns(df):
        s = df[col].dropna()
        if len(s) >= window:
            out[col] = getattr(s.rolling(window, min_periods=window), fn)() \
                .reindex(df.index)
    return out


def _diff_gapless(df: pd.DataFrame) -> pd.DataFrame:
    """差分以「前一個有效值」計算（跨越停牌缺口）。"""
    out = df.diff()
    for col in _gap_columns(df):
        out[col] = df[col].dropna().diff().reindex(df.index)
    return out


def compute(pv: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """回傳各指標矩陣。KD(9,3,3)、RSI(14)、MACD(12,26,9)、布林(20,2)、乖離率(20)。"""
    c, h, l = pv["close"], pv["high"], pv["low"]
    out: dict[str, pd.DataFrame] = {}

    # KD(9,3,3)：K = 前K*2/3 + RSV/3（等價於 ewm alpha=1/3）
    # 窗與遞迴都以實際交易日計，停牌缺口不影響（與逐日迴圈演算一致）
    low9 = _rolling_gapless(l, 9, "min")
    high9 = _rolling_gapless(h, 9, "max")
    span = (high9 - low9)
    rsv = ((c - low9) / span.where(span > 0) * 100).clip(0, 100)
    rsv = rsv.where(c.notna())
    rsv = rsv.mask(c.notna() & rsv.isna(), 50.0)  # 有交易但暖身窗未滿 → 以中值起算
    out["k"] = rsv.ewm(alpha=1 / 3, adjust=False, ignore_na=True).mean()
    out["d"] = out["k"].ewm(alpha=1 / 3, adjust=False, ignore_na=True).mean()

    # RSI(14)，Wilder 平滑（差分與遞迴均跳過停牌缺口）
    diff = _diff_gapless(c)
    gain = diff.clip(lower=0).ewm(alpha=1 / 14, adjust=False, ignore_na=True).mean()
    loss = (-diff.clip(upper=0)).ewm(alpha=1 / 14, adjust=False, ignore_na=True).mean()
    rs = gain / loss.where(loss > 0)
    out["rsi"] = (100 - 100 / (1 + rs)).fillna(50)

    # MACD(12,26,9)
    ema12 = c.ewm(span=12, adjust=False, ignore_na=True).mean()
    ema26 = c.ewm(span=26, adjust=False, ignore_na=True).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False, ignore_na=True).mean()
    out["macd"] = macd
    out["macd_signal"] = signal
    out["macd_hist"] = macd - signal

    # 布林通道(20, 2σ) 與 20 日乖離率
    mid = c.rolling(20).mean()
    sd = c.rolling(20).std()
    out["bb_mid"] = mid
    out["bb_up"] = mid + 2 * sd
    out["bb_low"] = mid - 2 * sd
    out["bias20"] = (c / mid - 1) * 100

    return out


def condition_matrices(pv: dict, ind: dict, ma_periods=(5, 10, 20)) -> dict[str, pd.DataFrame]:
    """回測與篩選共用的布林條件矩陣（True=當日該股符合）。"""
    c, v = pv["close"], pv["volume"]
    cond: dict[str, pd.DataFrame] = {}

    mas = {p: c.rolling(p).mean() for p in ma_periods}
    above = None
    for p in ma_periods:
        m = c > mas[p]
        above = m if above is None else (above & m)
    cond["above_all"] = above

    ps = sorted(ma_periods)
    bull = None
    for a, b in zip(ps, ps[1:]):
        m = mas[a] > mas[b]
        bull = m if bull is None else (bull & m)
    cond["bullish"] = bull if bull is not None else above

    cond["kd_cross"] = (ind["k"] > ind["d"]) & (ind["k"].shift(1) <= ind["d"].shift(1))
    cond["macd_flip"] = (ind["macd_hist"] > 0) & (ind["macd_hist"].shift(1) <= 0)
    cond["bb_break"] = c > ind["bb_up"]
    cond["volume_lot"] = v / 1000  # 數值矩陣，供門檻比較
    return cond
