"""盤中 K 棒監控引擎。

以 MIS 即時快照（約每 20 秒）重建 1 分／5 分 K 棒：
每分鐘第一個取樣價視為開盤、最後一個為收盤。
「收紅」＝該根 K 棒收盤 > 開盤（嚴格大於）。
偵測「1 分 K 連續 N 根收紅」與「5 分 K 連續 M 根收紅」（可 AND / OR 組合），
觸發後透過 LINE／Windows 通知，同一檔股票有冷卻時間避免轟炸。
"""
import json
from datetime import datetime

from . import db

CONFIG_PATH = db.DATA_DIR / "intraday_config.json"
DEFAULT_CONFIG = {
    "enabled": False,
    "watchlist": "",        # 監控的自選股清單名稱；空字串 = 所有自選股清單聯集
    "k1_count": 3,          # 1分K 連續收紅根數
    "k5_count": 2,          # 5分K 連續收紅根數
    "mode": "AND",          # AND=兩條件同時成立 / OR=任一成立
    "green_enabled": False,
    "k1_green_count": 3,    # 1分K 連續收綠根數
    "k5_green_count": 2,    # 5分K 連續收綠根數
    "green_mode": "AND",
    "line": True,
    "windows": True,
    "cooldown_min": 30,     # 同一檔觸發後的冷卻分鐘數
    "poll_sec": 20,
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return cfg


def save_config(cfg: dict):
    db.DATA_DIR.mkdir(exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                           encoding="utf-8")


class BarBuilder:
    """從逐次快照累積出 1 分 K（open/close），5 分 K 由 1 分 K 聚合。"""

    def __init__(self):
        # code -> {"HH:MM": [open, close]}
        self.bars: dict[str, dict[str, list[float]]] = {}

    def add(self, code: str, ts: datetime, price: float):
        mk = ts.strftime("%H:%M")
        bar = self.bars.setdefault(code, {}).setdefault(mk, [price, price])
        bar[1] = price

    def completed_1m(self, code: str, now: datetime) -> list[tuple[str, float, float]]:
        """已完成（非當前分鐘）的 1 分 K，依時間排序 → [(HH:MM, open, close)]"""
        cur = now.strftime("%H:%M")
        bars = self.bars.get(code, {})
        return [(k, v[0], v[1]) for k, v in sorted(bars.items()) if k < cur]

    def completed_5m(self, code: str, now: datetime) -> list[tuple[str, float, float]]:
        """已完成的 5 分 K：以 5 分鐘為桶聚合 1 分 K（開=桶內首根開、收=末根收）。
        只納入「桶的最後一分鐘已完成」的桶。"""
        m1 = self.completed_1m(code, now)
        if not m1:
            return []
        buckets: dict[str, list[float]] = {}
        for key, o, c in m1:
            h, m = key.split(":")
            bk = f"{h}:{int(m) // 5 * 5:02d}"
            bar = buckets.setdefault(bk, [o, c])
            bar[1] = c
        # 目前所屬的 5 分桶尚未完成 → 排除
        cur_bucket = f"{now:%H}:{now.minute // 5 * 5:02d}"
        return [(k, v[0], v[1]) for k, v in sorted(buckets.items()) if k < cur_bucket]


def _consecutive_red(bars: list[tuple[str, float, float]], n: int) -> bool:
    """最後 n 根皆收紅（收盤嚴格大於開盤）。"""
    if n <= 0:
        return True
    if len(bars) < n:
        return False
    return all(c > o for _, o, c in bars[-n:])

def _consecutive_green(bars: list[tuple[str, float, float]], n: int) -> bool:
    """最後 n 根皆收綠（收盤嚴格小於開盤）。"""
    if n <= 0:
        return True
    if len(bars) < n:
        return False
    return all(c < o for _, o, c in bars[-n:])



def check_signal(code: str, builder: BarBuilder, cfg: dict, now: datetime) -> str | None:
    """符合條件時回傳描述文字，否則 None。"""
    red1 = _consecutive_red(builder.completed_1m(code, now), cfg["k1_count"])
    red5 = _consecutive_red(builder.completed_5m(code, now), cfg["k5_count"])
    if cfg.get("mode", "AND") == "AND":
        hit = red1 and red5
    else:
        hit = red1 or red5
    if hit:
        parts = []
        if red1:
            parts.append(f"1分K連{cfg['k1_count']}紅")
        if red5:
            parts.append(f"5分K連{cfg['k5_count']}紅")
        return "＋".join(parts)

    if cfg.get("green_enabled", False):
        green1 = _consecutive_green(
            builder.completed_1m(code, now), cfg["k1_green_count"])
        green5 = _consecutive_green(
            builder.completed_5m(code, now), cfg["k5_green_count"])
        if cfg.get("green_mode", "AND") == "AND":
            green_hit = green1 and green5
        else:
            green_hit = green1 or green5
        if green_hit:
            parts = []
            if green1:
                parts.append(f"1分K連{cfg['k1_green_count']}綠")
            if green5:
                parts.append(f"5分K連{cfg['k5_green_count']}綠")
            return "＋".join(parts)
    return None


def resolve_codes(con, cfg: dict) -> list[tuple[str, str]]:
    """要監控的 (code, market) 清單。"""
    wl = cfg.get("watchlist") or ""
    if wl:
        codes = set(db.load_watchlist(con, wl))
    else:
        codes = set()
        for name in db.watchlist_names(con):
            codes |= set(db.load_watchlist(con, name))
    if not codes:
        return []
    rows = con.execute("SELECT code, market FROM stocks").fetchall()
    return [(c, m) for c, m in rows if c in codes]
