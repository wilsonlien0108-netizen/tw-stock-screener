"""盤中 K 棒監控常駐程式（開盤 09:00 前啟動，13:32 自動結束）。

用法：
    py scripts/intraday_monitor.py           # 正常執行（交易時段外會直接結束）
    py scripts/intraday_monitor.py --force   # 忽略時段限制（測試用）
    py scripts/intraday_monitor.py --test    # 發送一則模擬觸發通知後結束
"""
import sys
import time
from datetime import datetime, time as dtime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import db, intraday, notify  # noqa: E402
from src.sources import mis  # noqa: E402

LOG = db.DATA_DIR / "intraday_monitor.log"
MARKET_OPEN = dtime(9, 0)
MARKET_CLOSE = dtime(13, 32)


def log(msg: str):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def notify_hit(con, cfg, code: str, name: str, desc: str, price, prev_close):
    chg = f"（{(price / prev_close - 1) * 100:+.1f}%）" if prev_close else ""
    icon = "📉" if "綠" in desc else "🔥"
    msg = f"{icon} {code} {name} {desc}｜現價 {price}{chg}"
    if cfg.get("windows"):
        notify.windows_toast("盤中K棒訊號", msg)
    if cfg.get("line"):
        token = db.get_meta(con, "line_token") or ""
        uid = db.get_meta(con, "line_user_id") or ""
        if token and uid:
            ok, err = notify.line_push(token, uid, msg)
            if not ok:
                log(f"LINE 推播失敗：{err}")
        else:
            log("LINE 未設定 token/user ID，僅桌面通知")
    log(f"觸發：{msg}")


def main():
    force = "--force" in sys.argv
    cfg = intraday.load_config()
    if "--test" in sys.argv:
        con = db.connect()
        notify_hit(con, cfg, "2330", "台積電",
                   f"1分K連{cfg['k1_green_count']}綠＋"
                   f"5分K連{cfg['k5_green_count']}綠【空方測試】",
                   2350.0, 2411.0)
        log("測試通知已送出，結束。")
        return
    if not cfg.get("enabled") and not force:
        log("盤中監控未啟用（請在 App「資料與設定」開啟），結束。")
        return
    now = datetime.now()
    if not force and (now.weekday() >= 5 or now.time() >= MARKET_CLOSE):
        log("非交易時段，結束。")
        return

    con = db.connect()
    pairs = intraday.resolve_codes(con, cfg)
    if not pairs:
        log("沒有可監控的股票（自選股清單是空的），結束。")
        return
    names = dict(con.execute("SELECT code, name FROM stocks").fetchall())
    green_desc = (f"｜綠：1分K連{cfg['k1_green_count']}綠 "
                  f"{cfg.get('green_mode', 'AND')} 5分K連{cfg['k5_green_count']}綠"
                  if cfg.get("green_enabled") else "")
    log(f"開始監控 {len(pairs)} 檔｜紅：1分K連{cfg['k1_count']}紅 "
        f"{cfg['mode']} 5分K連{cfg['k5_count']}紅{green_desc}｜"
        f"每 {cfg['poll_sec']} 秒取樣，冷卻 {cfg['cooldown_min']} 分鐘")

    builder = intraday.BarBuilder()
    cooldown: dict[str, datetime] = {}
    last_eval_minute = ""
    while True:
        now = datetime.now()
        if not force and now.time() >= MARKET_CLOSE:
            log("收盤，監控結束。")
            break
        if force or now.time() >= MARKET_OPEN:
            try:
                quotes = mis.fetch_quotes(pairs)
            except Exception as e:  # noqa: BLE001 - 單次失敗下輪重試
                log(f"取價失敗：{e}")
                quotes = {}
            ts = datetime.now()
            for code, q in quotes.items():
                if q.get("price"):
                    builder.add(code, ts, q["price"])
            # 每分鐘切換時評估一次（用已完成的 K 棒）
            cur_minute = ts.strftime("%H:%M")
            if cur_minute != last_eval_minute:
                last_eval_minute = cur_minute
                for code, q in quotes.items():
                    cd = cooldown.get(code)
                    if cd and (ts - cd).total_seconds() < cfg["cooldown_min"] * 60:
                        continue
                    desc = intraday.check_signal(code, builder, cfg, ts)
                    if desc:
                        notify_hit(con, cfg, code, names.get(code, ""), desc,
                                   q.get("price"), q.get("prev_close"))
                        cooldown[code] = ts
        time.sleep(cfg["poll_sec"])


if __name__ == "__main__":
    main()
