"""雲端模式支援。

架構：家用電腦每日更新資料後，把資料庫快照上傳到 GitHub Release（tag=data）；
Streamlit Cloud 上的 App 啟動／定期檢查該檔案，有新版就下載替換本地 DB。
雲端判定：Streamlit secrets 內設定了 data_url 即為雲端模式。
"""
import json
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from . import db
from .sources.http import session

SYNC_MARKER = db.DATA_DIR / "cloud_sync.json"
CHECK_INTERVAL_MIN = 30
SENSITIVE_META_TERMS = ("token", "secret", "password", "credential", "api_key")


def _sanitize_snapshot(path: Path) -> list[str]:
    """Remove credentials and personal delivery identifiers from a public snapshot."""
    con = sqlite3.connect(path)
    try:
        keys = [row[0] for row in con.execute("SELECT key FROM meta").fetchall()]
        sensitive = [
            key for key in keys
            if key == "line_user_id"
            or any(term in key.lower() for term in SENSITIVE_META_TERMS)
        ]
        con.executemany("DELETE FROM meta WHERE key=?", ((key,) for key in sensitive))
        con.commit()
        return sensitive
    finally:
        con.close()



def data_url() -> str | None:
    """雲端模式的資料來源網址（設定於 Streamlit secrets）。本機執行回傳 None。"""
    if os.environ.get("SCREENER_DATA_URL"):
        return os.environ["SCREENER_DATA_URL"]
    try:
        import streamlit as st
        return st.secrets.get("data_url")
    except Exception:  # noqa: BLE001 - 無 secrets 檔即本機模式
        return None


def _load_marker() -> dict:
    if SYNC_MARKER.exists():
        try:
            return json.loads(SYNC_MARKER.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def _save_marker(m: dict):
    db.DATA_DIR.mkdir(exist_ok=True)
    SYNC_MARKER.write_text(json.dumps(m), encoding="utf-8")


def _remote_stamp(url: str) -> str | None:
    """遠端檔案版本戳（Last-Modified / ETag）。"""
    try:
        r = session().head(url, timeout=20, allow_redirects=True)
        return r.headers.get("Last-Modified") or r.headers.get("ETag")
    except Exception:  # noqa: BLE001
        return None


def _download(url: str) -> bool:
    tmp = db.DB_PATH.with_suffix(".download")
    try:
        with session().get(url, timeout=600, stream=True, allow_redirects=True) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
        with open(tmp, "rb") as f:
            if f.read(16) != b"SQLite format 3\x00":  # 完整性檢查
                tmp.unlink()
                return False
        os.replace(tmp, db.DB_PATH)
        return True
    except Exception:  # noqa: BLE001
        if tmp.exists():
            tmp.unlink()
        return False


def sync_db(url: str, force: bool = False) -> str:
    """確保本地 DB 為最新。回傳狀態說明文字。"""
    marker = _load_marker()
    now = time.time()
    if not db.DB_PATH.exists():
        ok = _download(url)
        if ok:
            _save_marker({"stamp": _remote_stamp(url), "checked": now})
            return "已下載最新資料"
        return "❌ 資料下載失敗，請稍後重新整理"
    if not force and now - marker.get("checked", 0) < CHECK_INTERVAL_MIN * 60:
        return f"資料同步於 {datetime.fromtimestamp(marker.get('checked', now)):%H:%M}"
    stamp = _remote_stamp(url)
    marker["checked"] = now
    if stamp and stamp != marker.get("stamp"):
        if _download(url):
            marker["stamp"] = stamp
            _save_marker(marker)
            return "已更新到最新資料"
    _save_marker(marker)
    return "資料已是最新"


# ---------- 發佈端（家用電腦） ----------

PUBLISH_CONFIG = db.DATA_DIR / "cloud_config.json"
API = "https://api.github.com"


def load_publish_config() -> dict:
    if PUBLISH_CONFIG.exists():
        try:
            return json.loads(PUBLISH_CONFIG.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def save_publish_config(repo: str, token: str):
    PUBLISH_CONFIG.write_text(
        json.dumps({"repo": repo.strip(), "token": token.strip()}),
        encoding="utf-8")


def publish(progress=None) -> str:
    """把資料庫快照上傳到 GitHub Release（tag=data）。回傳結果說明。"""
    cfg = load_publish_config()
    repo, token = cfg.get("repo"), cfg.get("token")
    if not (repo and token):
        return "未設定雲端發佈（需 GitHub repo 與 token）"

    def log(msg):
        if progress:
            progress(msg)

    headers = {"Authorization": f"Bearer {token}",
               "Accept": "application/vnd.github+json",
               "User-Agent": "tw-stock-screener"}
    s = session()

    # 1. 一致性快照（VACUUM INTO 也會瘦身）
    log("建立資料庫快照…")
    snap = db.DATA_DIR / "publish_snapshot.db"
    if snap.exists():
        snap.unlink()
    con = db.connect()
    con.execute("VACUUM INTO ?", (str(snap),))
    con.close()
    removed = _sanitize_snapshot(snap)
    if removed:
        log(f"已移除 {len(removed)} 個本機憑證／識別設定…")
    size_mb = snap.stat().st_size / 1048576

    try:
        # 2. 取得（或建立）tag=data 的 release
        r = s.get(f"{API}/repos/{repo}/releases/tags/data", headers=headers,
                  timeout=30)
        if r.status_code == 404:
            r = s.post(f"{API}/repos/{repo}/releases", headers=headers, timeout=30,
                       json={"tag_name": "data", "name": "每日資料",
                             "body": "由篩選器自動更新的資料庫快照"})
        if r.status_code not in (200, 201):
            return f"取得 Release 失敗：HTTP {r.status_code} {r.text[:150]}"
        rel = r.json()

        # 3. 刪除舊資產、上傳新檔
        for asset in rel.get("assets", []):
            if asset["name"] == "screener.db":
                s.delete(f"{API}/repos/{repo}/releases/assets/{asset['id']}",
                         headers=headers, timeout=30)
        log(f"上傳資料庫（{size_mb:.0f} MB）…")
        with open(snap, "rb") as f:
            r = s.post(
                f"https://uploads.github.com/repos/{repo}/releases/{rel['id']}"
                f"/assets?name=screener.db",
                headers={**headers, "Content-Type": "application/octet-stream"},
                data=f, timeout=1800)
        if r.status_code != 201:
            return f"上傳失敗：HTTP {r.status_code} {r.text[:150]}"
        url = f"https://github.com/{repo}/releases/download/data/screener.db"
        return f"✅ 發佈成功（{size_mb:.0f} MB）→ {url}"
    finally:
        if snap.exists():
            snap.unlink()
