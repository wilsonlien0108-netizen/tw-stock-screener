"""概念股清單管理：讀寫 config/concepts.json、從網頁匯入代號。"""
import json
import re
from pathlib import Path

from .sources.http import get

CONFIG = Path(__file__).resolve().parent.parent / "config" / "concepts.json"


def load() -> dict[str, list[str]]:
    if CONFIG.exists():
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    return {}


def save(data: dict[str, list[str]]):
    CONFIG.parent.mkdir(exist_ok=True)
    CONFIG.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def as_sets(data: dict[str, list[str]] | None = None) -> dict[str, set]:
    return {k: set(v) for k, v in (data or load()).items()}


def extract_codes_from_url(url: str, universe: set[str]) -> list[str]:
    """抓網頁內容，萃取所有出現、且存在於股票清單中的 4 碼股號（去重、保序）。"""
    r = get(url, timeout=30)
    r.encoding = r.apparent_encoding or "utf-8"
    found = re.findall(r"(?<![0-9A-Za-z])(\d{4})(?![0-9])", r.text)
    seen, out = set(), []
    for c in found:
        if c in universe and c not in seen:
            seen.add(c)
            out.append(c)
    return out
