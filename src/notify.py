"""推播通知：Windows 桌面通知（PowerShell WinRT，零依賴）與 LINE Messaging API。"""
import base64
import json
import subprocess

from .sources.http import session

_PS_TOAST = r"""
$t=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{B64TITLE}'))
$b=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{B64BODY}'))
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] | Out-Null
$x = New-Object Windows.Data.Xml.Dom.XmlDocument
$x.LoadXml('<toast duration="long"><visual><binding template="ToastGeneric"><text></text><text></text></binding></visual></toast>')
$texts = $x.GetElementsByTagName('text')
$null = $texts.Item(0).AppendChild($x.CreateTextNode($t))
$null = $texts.Item(1).AppendChild($x.CreateTextNode($b))
$appid = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
$toast = New-Object Windows.UI.Notifications.ToastNotification($x)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appid).Show($toast)
"""


def _b64(s: str) -> str:
    return base64.b64encode(s.encode("utf-8")).decode()


def windows_toast(title: str, body: str) -> tuple[bool, str]:
    """顯示 Windows 桌面通知。標題/內文以 base64 傳遞，避免任何跳脫問題。"""
    ps = _PS_TOAST.replace("{B64TITLE}", _b64(title)).replace("{B64BODY}", _b64(body))
    encoded = base64.b64encode(ps.encode("utf-16-le")).decode()
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-EncodedCommand", encoded],
                           capture_output=True, timeout=30)
        if r.returncode == 0:
            return True, ""
        return False, (r.stderr or b"").decode("utf-8", errors="replace")[:200]
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def line_push(channel_token: str, user_id: str, text: str) -> tuple[bool, str]:
    """LINE Messaging API push。user_id 可用逗號/空白分隔多個收件人（multicast）。
    需免費申請官方帳號取得 channel access token 與 user ID。"""
    import re
    ids = [i for i in re.split(r"[,\s;]+", user_id or "") if i]
    if not ids:
        return False, "未提供 user ID"
    try:
        if len(ids) == 1:
            endpoint = "https://api.line.me/v2/bot/message/push"
            payload = {"to": ids[0],
                       "messages": [{"type": "text", "text": text[:4900]}]}
        else:
            endpoint = "https://api.line.me/v2/bot/message/multicast"
            payload = {"to": ids[:500],
                       "messages": [{"type": "text", "text": text[:4900]}]}
        r = session().post(endpoint,
                           headers={"Authorization": f"Bearer {channel_token}"},
                           json=payload, timeout=30)
        if r.status_code == 200:
            return True, ""
        return False, f"HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def format_results(df, date_str: str, top_n: int = 15) -> str:
    """把篩選結果 DataFrame 組成一則推播文字。"""
    import pandas as pd
    lines = [f"📈 台股篩選 {date_str}：通過 {len(df)} 檔"]
    for _, r in df.head(top_n).iterrows():
        chg = r.get("change_pct")
        chg_s = f"{chg:+.1f}%" if not pd.isna(chg) else ""
        fn = r.get("foreign_net")
        fn_s = f" 外資{fn:+,.0f}張" if not pd.isna(fn) else ""
        lines.append(f"{r['code']} {r['name']} {r['close']:.2f} {chg_s}{fn_s}")
    if len(df) > top_n:
        lines.append(f"…及其他 {len(df) - top_n} 檔")
    lines.append("—— 台股均線籌碼篩選器")
    return "\n".join(lines)


def load_push_config(con) -> dict:
    from . import db
    raw = db.get_meta(con, "push_config")
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
    return {"windows": False, "line": False, "params": None,
            "ma_periods": [5, 10, 20], "big_threshold": 400, "top_n": 15}


def save_push_config(con, cfg: dict):
    from . import db
    db.set_meta(con, "push_config", json.dumps(cfg, ensure_ascii=False))


def _screen_once(con, params: dict, ma_periods, big_threshold):
    from . import db, screener
    from . import concepts as cp
    params = dict(params)
    if params.get("watchlist"):
        params["watchlist_codes"] = set(db.load_watchlist(con, params["watchlist"]))
    table = screener.build_table(con, tuple(ma_periods), None, big_threshold)
    if table.empty:
        return None, None
    hit = screener.apply_filters(table, params, cp.as_sets())
    hit = hit.sort_values("total_net", ascending=False, na_position="last")
    return hit, str(table["last_date"].iloc[0])


def push_screen_results(con) -> list[str]:
    """依儲存的推播設定執行篩選並推送（支援多策略）。回傳訊息紀錄。"""
    from . import db
    logs = []
    cfg = load_push_config(con)
    if not (cfg.get("windows") or cfg.get("line")):
        return ["推播未啟用"]

    # 組推播內容：多策略各一節；無策略清單則退回舊版單一條件
    sections = []
    strategies = cfg.get("strategies") or []
    if strategies:
        for name in strategies:
            raw = db.load_strategy(con, name)
            if not raw:
                logs.append(f"策略「{name}」不存在，略過")
                continue
            s = json.loads(raw)
            hit, date_str = _screen_once(con, s.get("params", {}),
                                         s.get("ma_periods", [5, 10, 20]),
                                         s.get("big_threshold", 400))
            if hit is None:
                continue
            sections.append((name, hit, date_str))
    elif cfg.get("params"):
        hit, date_str = _screen_once(con, cfg["params"],
                                     cfg.get("ma_periods", [5, 10, 20]),
                                     cfg.get("big_threshold", 400))
        if hit is not None:
            sections.append(("篩選條件", hit, date_str))
    if not sections:
        return logs + ["沒有可推播的內容（未設定策略或條件）"]

    date_str = sections[0][2]
    top_n = cfg.get("top_n", 15)
    parts = []
    toast_lines = []
    for name, hit, _ in sections:
        parts.append(f"【{name}】" + format_results(hit, date_str, top_n))
        toast_lines.append(f"{name}：{len(hit)} 檔（" +
                           "、".join(hit["name"].head(5)) +
                           ("…" if len(hit) > 5 else "") + "）")
    text = "\n\n".join(parts)

    if cfg.get("windows"):
        ok, err = windows_toast(f"台股篩選 {date_str}", "\n".join(toast_lines)[:200])
        logs.append(f"Windows 通知：{'成功' if ok else '失敗 ' + err}")
    if cfg.get("line"):
        token = db.get_meta(con, "line_token") or ""
        uid = db.get_meta(con, "line_user_id") or ""
        if token and uid:
            ok, err = line_push(token, uid, text)
            logs.append(f"LINE 推播：{'成功' if ok else '失敗 ' + err}")
        else:
            logs.append("LINE 推播：未設定 token/user ID，略過")
    return logs


def push_failure_alert(con, error_msg: str):
    """資料更新失敗時的告警（Windows 一定發、LINE 依設定）。"""
    from . import db
    windows_toast("⚠️ 台股篩選器：資料更新失敗", error_msg[:180])
    cfg = load_push_config(con)
    if cfg.get("line"):
        token = db.get_meta(con, "line_token") or ""
        uid = db.get_meta(con, "line_user_id") or ""
        if token and uid:
            line_push(token, uid, f"⚠️ 台股篩選器資料更新失敗：{error_msg[:300]}")
