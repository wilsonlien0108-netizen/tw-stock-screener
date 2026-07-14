"""盤後排程更新：抓最近缺漏的股價/法人/基本面資料與集保週資料，完成後依設定推播。

用法：
    py scripts/daily_update.py            # 增量更新（最近 10 個交易日缺漏）
    py scripts/daily_update.py --full     # 完整回補 60 個交易日
    py scripts/daily_update.py --year     # 回補一年（250 交易日，供回測，約 40-50 分鐘）
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import db, updater  # noqa: E402

LOG = db.DATA_DIR / "update.log"


def main():
    full = "--full" in sys.argv
    year = "--year" in sys.argv
    price_days, insti_days = (250, 250) if year else (60, 15) if full else (10, 10)
    con = db.connect()
    lines = [f"===== {datetime.now():%Y-%m-%d %H:%M:%S} 開始更新 "
             f"(price={price_days}, insti={insti_days}) ====="]

    def progress(msg, frac=None):
        line = f"[{datetime.now():%H:%M:%S}] {msg}"
        print(line)
        lines.append(line)

    try:
        result = updater.update_all(con, price_days, insti_days, progress)
        lines.append(f"結果：{result}")
        try:
            b = updater.backup_db(con)
            if b:
                progress(f"已備份資料庫：{b}")
            pruned = updater.prune_old_data(con)
            if pruned:
                progress(f"已修剪 {pruned} 筆超過兩年的舊資料")
        except Exception as e:  # noqa: BLE001 - 維護失敗不影響更新
            lines.append(f"備份/修剪錯誤：{e}")
        if not year:  # 一年回補是背景任務，不觸發推播
            try:
                from src import notify
                for line in notify.push_screen_results(con):
                    progress(f"推播：{line}")
            except Exception as e:  # noqa: BLE001 - 推播失敗不影響更新
                lines.append(f"推播錯誤：{e}")
        try:
            from src import cloud
            if cloud.load_publish_config():
                progress("發佈資料到雲端…")
                progress(cloud.publish(progress))
        except Exception as e:  # noqa: BLE001 - 雲端發佈失敗不影響更新
            lines.append(f"雲端發佈錯誤：{e}")
    except Exception as e:
        lines.append(f"錯誤：{e}")
        try:
            from src import notify
            notify.push_failure_alert(con, str(e))
            lines.append("已發送更新失敗告警")
        except Exception:  # noqa: BLE001
            pass
        raise
    finally:
        LOG.parent.mkdir(exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
