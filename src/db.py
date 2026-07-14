"""SQLite 資料庫：schema 與共用查詢。"""
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "screener.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS stocks(
  code TEXT PRIMARY KEY,
  name TEXT,
  market TEXT,             -- TWSE / TPEX
  industry_code TEXT,
  industry_name TEXT
);
CREATE TABLE IF NOT EXISTS prices(
  code TEXT, date TEXT,
  open REAL, high REAL, low REAL, close REAL, volume INTEGER,
  PRIMARY KEY(code, date)
);
CREATE INDEX IF NOT EXISTS idx_prices_date ON prices(date);
CREATE TABLE IF NOT EXISTS insti(
  code TEXT, date TEXT,
  foreign_net INTEGER,     -- 外資+外資自營 買賣超（股）
  trust_net INTEGER,       -- 投信（股）
  dealer_net INTEGER,      -- 自營商合計（股）
  total_net INTEGER,       -- 三大法人合計（股）
  PRIMARY KEY(code, date)
);
CREATE INDEX IF NOT EXISTS idx_insti_date ON insti(date);
CREATE TABLE IF NOT EXISTS tdcc(
  code TEXT, date TEXT, level INTEGER,
  holders INTEGER, shares INTEGER, pct REAL,
  PRIMARY KEY(code, date, level)
);
CREATE INDEX IF NOT EXISTS idx_tdcc_date ON tdcc(date);
CREATE TABLE IF NOT EXISTS source_health(
  dataset TEXT, source TEXT,
  status TEXT,             -- ok / fail
  message TEXT,
  updated_at TEXT,
  PRIMARY KEY(dataset, source)
);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS watchlists(
  name TEXT, code TEXT, added_at TEXT,
  PRIMARY KEY(name, code)
);
CREATE TABLE IF NOT EXISTS revenue(
  code TEXT, month TEXT,      -- YYYY-MM
  revenue INTEGER,            -- 千元
  mom REAL, yoy REAL, cum_yoy REAL,
  PRIMARY KEY(code, month)
);
CREATE TABLE IF NOT EXISTS valuation(
  code TEXT, date TEXT,
  pe REAL, dividend_yield REAL, pb REAL,
  PRIMARY KEY(code, date)
);
CREATE TABLE IF NOT EXISTS margin(
  code TEXT, date TEXT,
  fin_balance INTEGER, fin_chg INTEGER,     -- 融資餘額/日增減（張）
  short_balance INTEGER, short_chg INTEGER, -- 融券餘額/日增減（張）
  PRIMARY KEY(code, date)
);
CREATE INDEX IF NOT EXISTS idx_margin_date ON margin(date);
CREATE TABLE IF NOT EXISTS index_daily(
  date TEXT PRIMARY KEY, taiex REAL
);
CREATE TABLE IF NOT EXISTS strategies(
  name TEXT PRIMARY KEY, data TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS alerts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT, kind TEXT, threshold REAL,
  enabled INTEGER DEFAULT 1, triggered_date TEXT
);
"""

# 證交所/櫃買（公開資訊觀測站）產業別代碼
INDUSTRY = {
    "01": "水泥", "02": "食品", "03": "塑膠", "04": "紡織纖維", "05": "電機機械",
    "06": "電器電纜", "08": "玻璃陶瓷", "09": "造紙", "10": "鋼鐵", "11": "橡膠",
    "12": "汽車", "14": "建材營造", "15": "航運", "16": "觀光餐旅", "17": "金融保險",
    "18": "貿易百貨", "19": "綜合", "20": "其他", "21": "化學", "22": "生技醫療",
    "23": "油電燃氣", "24": "半導體", "25": "電腦及週邊", "26": "光電",
    "27": "通信網路", "28": "電子零組件", "29": "電子通路", "30": "資訊服務",
    "31": "其他電子", "32": "文化創意", "33": "農業科技", "34": "電子商務",
    "35": "綠能環保", "36": "數位雲端", "37": "運動休閒", "38": "居家生活",
}
# 電子類股 = 電子工業八大官方子產業
ELECTRONIC_CODES = {"24", "25", "26", "27", "28", "29", "30", "31"}


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.executescript(SCHEMA)
    return con


def upsert_many(con, table: str, cols: list[str], rows):
    rows = list(rows)
    if not rows:
        return 0
    ph = ",".join("?" * len(cols))
    con.executemany(
        f"INSERT OR REPLACE INTO {table}({','.join(cols)}) VALUES({ph})", rows)
    con.commit()
    return len(rows)


def get_meta(con, key: str, default=None):
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(con, key: str, value: str):
    con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, value))
    con.commit()


def record_health(con, dataset: str, source: str, ok: bool, message: str = ""):
    con.execute(
        "INSERT OR REPLACE INTO source_health(dataset,source,status,message,updated_at)"
        " VALUES(?,?,?,?,?)",
        (dataset, source, "ok" if ok else "fail", message[:300],
         datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    con.commit()


def universe(con) -> pd.DataFrame:
    return pd.read_sql("SELECT * FROM stocks", con)


def price_dates(con) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT DISTINCT date FROM prices ORDER BY date").fetchall()]


def insti_dates(con) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT DISTINCT date FROM insti ORDER BY date").fetchall()]


def tdcc_dates(con) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT DISTINCT date FROM tdcc ORDER BY date").fetchall()]


def load_prices(con, last_n_dates: int) -> pd.DataFrame:
    dates = price_dates(con)[-last_n_dates:]
    if not dates:
        return pd.DataFrame(columns=["code", "date", "open", "high", "low", "close", "volume"])
    ph = ",".join("?" * len(dates))
    return pd.read_sql(
        f"SELECT * FROM prices WHERE date IN ({ph})", con, params=dates)


def load_insti(con, last_n_dates: int) -> pd.DataFrame:
    dates = insti_dates(con)[-last_n_dates:]
    if not dates:
        return pd.DataFrame(columns=["code", "date", "foreign_net", "trust_net",
                                     "dealer_net", "total_net"])
    ph = ",".join("?" * len(dates))
    return pd.read_sql(
        f"SELECT * FROM insti WHERE date IN ({ph})", con, params=dates)


def load_tdcc(con, last_n_dates: int = 2) -> pd.DataFrame:
    dates = tdcc_dates(con)[-last_n_dates:]
    if not dates:
        return pd.DataFrame(columns=["code", "date", "level", "holders", "shares", "pct"])
    ph = ",".join("?" * len(dates))
    return pd.read_sql(
        f"SELECT * FROM tdcc WHERE date IN ({ph})", con, params=dates)


def load_stock_history(con, code: str, last_n_dates: int = 120) -> pd.DataFrame:
    return pd.read_sql(
        "SELECT * FROM prices WHERE code=? ORDER BY date DESC LIMIT ?",
        con, params=(code, last_n_dates)).sort_values("date")


def load_stock_insti(con, code: str, last_n_dates: int = 60) -> pd.DataFrame:
    return pd.read_sql(
        "SELECT * FROM insti WHERE code=? ORDER BY date DESC LIMIT ?",
        con, params=(code, last_n_dates)).sort_values("date")


def load_stock_tdcc(con, code: str) -> pd.DataFrame:
    return pd.read_sql(
        "SELECT * FROM tdcc WHERE code=? ORDER BY date", con, params=(code,))


def watchlist_names(con) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT DISTINCT name FROM watchlists ORDER BY name").fetchall()]


def load_watchlist(con, name: str) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT code FROM watchlists WHERE name=? ORDER BY code", (name,)).fetchall()]


def save_watchlist(con, name: str, codes: list[str]):
    con.execute("DELETE FROM watchlists WHERE name=?", (name,))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    con.executemany("INSERT OR REPLACE INTO watchlists(name,code,added_at) VALUES(?,?,?)",
                    [(name, c, now) for c in codes])
    con.commit()


def delete_watchlist(con, name: str):
    con.execute("DELETE FROM watchlists WHERE name=?", (name,))
    con.commit()


def load_latest_revenue(con) -> pd.DataFrame:
    """每檔股票最新一個月的營收（index=code）。"""
    df = pd.read_sql(
        "SELECT r.* FROM revenue r JOIN"
        " (SELECT code, MAX(month) AS m FROM revenue GROUP BY code) t"
        " ON r.code = t.code AND r.month = t.m", con)
    return df.set_index("code") if not df.empty else df


def load_latest_valuation(con) -> pd.DataFrame:
    df = pd.read_sql(
        "SELECT v.* FROM valuation v JOIN"
        " (SELECT code, MAX(date) AS d FROM valuation GROUP BY code) t"
        " ON v.code = t.code AND v.date = t.d", con)
    return df.set_index("code") if not df.empty else df


def load_stock_revenue(con, code: str, months: int = 24) -> pd.DataFrame:
    return pd.read_sql(
        "SELECT * FROM revenue WHERE code=? ORDER BY month DESC LIMIT ?",
        con, params=(code, months)).sort_values("month")


def margin_dates(con) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT DISTINCT date FROM margin ORDER BY date").fetchall()]


def load_margin(con, last_n_dates: int) -> pd.DataFrame:
    dates = margin_dates(con)[-last_n_dates:]
    if not dates:
        return pd.DataFrame(columns=["code", "date", "fin_balance", "fin_chg",
                                     "short_balance", "short_chg"])
    ph = ",".join("?" * len(dates))
    return pd.read_sql(f"SELECT * FROM margin WHERE date IN ({ph})", con, params=dates)


def load_stock_margin(con, code: str, n: int = 60) -> pd.DataFrame:
    return pd.read_sql("SELECT * FROM margin WHERE code=? ORDER BY date DESC LIMIT ?",
                       con, params=(code, n)).sort_values("date")


def load_revenue_matrix(con, months: int = 25) -> pd.DataFrame:
    """月營收寬表（index=月份 asc, columns=code, values=revenue）。"""
    ms = [r[0] for r in con.execute(
        "SELECT DISTINCT month FROM revenue ORDER BY month DESC LIMIT ?",
        (months,)).fetchall()]
    if not ms:
        return pd.DataFrame()
    ph = ",".join("?" * len(ms))
    df = pd.read_sql(f"SELECT code, month, revenue, yoy FROM revenue "
                     f"WHERE month IN ({ph})", con, params=ms)
    return df


def load_index(con, n: int = 90) -> pd.DataFrame:
    return pd.read_sql("SELECT * FROM index_daily ORDER BY date DESC LIMIT ?",
                       con, params=(n,)).sort_values("date")


# ---- 策略 ----

def strategy_names(con) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT name FROM strategies ORDER BY name").fetchall()]


def save_strategy(con, name: str, data: str):
    con.execute("INSERT OR REPLACE INTO strategies(name,data,updated_at) VALUES(?,?,?)",
                (name, data, datetime.now().strftime("%Y-%m-%d %H:%M")))
    con.commit()


def load_strategy(con, name: str) -> str | None:
    r = con.execute("SELECT data FROM strategies WHERE name=?", (name,)).fetchone()
    return r[0] if r else None


def delete_strategy(con, name: str):
    con.execute("DELETE FROM strategies WHERE name=?", (name,))
    con.commit()


# ---- 盤中警示 ----

def list_alerts(con) -> pd.DataFrame:
    return pd.read_sql("SELECT * FROM alerts ORDER BY id", con)


def replace_alerts(con, df: pd.DataFrame):
    con.execute("DELETE FROM alerts")
    for _, r in df.iterrows():
        if not r.get("code"):
            continue
        con.execute(
            "INSERT INTO alerts(code,kind,threshold,enabled,triggered_date)"
            " VALUES(?,?,?,?,?)",
            (str(r["code"]).strip(), r["kind"], float(r["threshold"] or 0),
             int(bool(r.get("enabled", True))), r.get("triggered_date")))
    con.commit()


def mark_alert_triggered(con, alert_id: int, date_str: str):
    con.execute("UPDATE alerts SET triggered_date=? WHERE id=?", (date_str, alert_id))
    con.commit()


def health(con) -> pd.DataFrame:
    return pd.read_sql(
        "SELECT dataset AS 資料集, source AS 來源, status AS 狀態,"
        " message AS 訊息, updated_at AS 更新時間 FROM source_health"
        " ORDER BY dataset, source", con)
