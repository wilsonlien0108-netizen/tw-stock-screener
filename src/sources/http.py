"""共用 HTTP 層：使用 Windows 系統憑證庫（truststore）、UA、每主機節流與重試。"""
import time
import threading

import truststore

truststore.inject_into_ssl()

import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
}

_session = None
_lock = threading.Lock()
_last_call: dict[str, float] = {}


def session() -> requests.Session:
    global _session
    with _lock:
        if _session is None:
            _session = requests.Session()
            _session.headers.update(HEADERS)
        return _session


def _throttle(key: str, min_interval: float):
    if min_interval <= 0:
        return
    with _lock:
        last = _last_call.get(key, 0.0)
        wait = last + min_interval - time.time()
    if wait > 0:
        time.sleep(wait)
    with _lock:
        _last_call[key] = time.time()


def get(url: str, params=None, *, min_interval: float = 0.0, key: str | None = None,
        timeout: int = 30, retries: int = 2) -> requests.Response:
    """GET 並在失敗時退避重試。key 用於節流分組（預設用網域）。"""
    key = key or url.split("/")[2]
    last_exc = None
    for attempt in range(retries + 1):
        _throttle(key, min_interval)
        try:
            r = session().get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001 - 統一回報給呼叫端
            last_exc = e
            time.sleep(1.5 * (attempt + 1))
    raise last_exc


def get_json(url: str, params=None, **kw):
    return get(url, params, **kw).json()


def num(s):
    """把 '1,234'、'--'、'' 之類的字串轉成數字；轉不了回傳 None。"""
    if s is None:
        return None
    s = str(s).replace(",", "").replace("+", "").strip()
    if s in ("", "--", "---", "-", "X", "除息", "除權", "除權息", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None
