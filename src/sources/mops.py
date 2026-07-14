"""公開資訊觀測站（MOPS）月營收彙總靜態檔。

OpenAPI 的月營收檔缺少部分公司（含台積電、中華電等約 60 檔），
這裡改抓 MOPS 完整彙總檔（免註冊）：
  https://mopsov.twse.com.tw/nas/t21/{sii|otc}/t21sc03_{ROC年}_{月}_{0|1}.html
  kind 0=國內公司、1=國外(KY)公司。以正規表達式解析，零額外依賴。
"""
import re
from datetime import date

from .http import get

BASE = "https://mopsov.twse.com.tw/nas/t21"
_TR = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")


def _clean(s: str) -> str:
    return _TAG.sub("", s).replace("&nbsp;", " ").replace(",", "").strip()


def _num(s: str):
    s = s.strip()
    if s in ("", "-", "不適用", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def latest_month() -> tuple[int, int]:
    """最新應已公布的營收月份（每月 10 日申報截止，留 1 天緩衝）。"""
    t = date.today()
    y, m = t.year, t.month - 1
    if t.day < 11:
        m -= 1
    while m <= 0:
        m += 12
        y -= 1
    return y, m


def fetch_revenue(market: str, year: int, month: int) -> list[tuple]:
    """market: 'sii'(上市) 或 'otc'(上櫃)。
    回傳 (code, 'YYYY-MM', revenue千元, mom%, yoy%, cum_yoy%)。查無檔案回傳空 list。"""
    month_key = f"{year}-{month:02d}"
    rows: list[tuple] = []
    for kind in (0, 1):  # 國內 + 國外KY
        url = f"{BASE}/{market}/t21sc03_{year - 1911}_{month}_{kind}.html"
        try:
            r = get(url, min_interval=1.0, timeout=60)
        except Exception:
            continue  # 其中一檔不存在（例如當月 KY 檔尚未產出）不影響另一檔
        text = r.content.decode("big5", errors="replace")
        for tr in _TR.findall(text):
            tds = [_clean(x) for x in _TD.findall(tr)]
            if len(tds) < 10 or not re.fullmatch(r"\d{4}", tds[0]):
                continue
            rev = _num(tds[2])
            if rev is None:
                continue
            rows.append((tds[0], month_key, int(rev),
                         _num(tds[5]), _num(tds[6]), _num(tds[9])))
    return rows
