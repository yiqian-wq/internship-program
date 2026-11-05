"""基础设施：MySQL 连接、Elasticsearch 客户端、DDL/维表/索引模板初始化。

跨平台说明：连接参数来自 configs/infra.yaml，可被环境变量覆盖
（GAMEDW_MYSQL_HOST/PORT/USER/PASSWORD、GAMEDW_ES_URL），Windows/macOS/Linux 通用。
"""
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pymysql
import yaml
from elasticsearch import Elasticsearch

sys.path.insert(0, str(Path(__file__).parent))
from config import load_config  # noqa: E402

ROOT = Path(__file__).parent
DDL_PATH = ROOT / "etl" / "sql" / "00_init.sql"
INFRA_PATH = ROOT / "configs" / "infra.yaml"


def _utf8_console():
    """Windows 控制台默认 GBK，强制切 UTF-8 避免中文乱码（幂等）。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass


def load_infra() -> dict:
    infra = yaml.safe_load(INFRA_PATH.read_text(encoding="utf-8"))
    m = infra.setdefault("mysql", {})
    m["host"] = os.environ.get("GAMEDW_MYSQL_HOST", m["host"])
    m["port"] = int(os.environ.get("GAMEDW_MYSQL_PORT", m["port"]))
    m["user"] = os.environ.get("GAMEDW_MYSQL_USER", m["user"])
    m["password"] = os.environ.get("GAMEDW_MYSQL_PASSWORD", m.get("password", ""))
    infra.setdefault("elasticsearch", {})
    infra["elasticsearch"]["url"] = os.environ.get(
        "GAMEDW_ES_URL", infra["elasticsearch"].get("url", "http://127.0.0.1:9200"))
    return infra


def mysql_connect(database: str | None = None) -> pymysql.connections.Connection:
    m = load_infra()["mysql"]
    return pymysql.connect(
        host=m["host"], port=m["port"], user=m["user"], password=m["password"],
        database=database, charset="utf8mb4", autocommit=True,
        cursorclass=pymysql.cursors.Cursor,
    )


def es_client() -> Elasticsearch:
    url = load_infra()["elasticsearch"]["url"]
    return Elasticsearch(url, request_timeout=120)


# ---------------------------------------------------------------- MySQL 初始化

def run_sql_file(con, path: Path) -> None:
    """按分号切分执行 SQL 文件（文件内字符串不含分号）。"""
    sql = "\n".join(line for line in path.read_text(encoding="utf-8").splitlines()
                    if not line.strip().startswith("--"))
    for stmt in (s.strip() for s in sql.split(";")):
        if stmt:
            with con.cursor() as c:
                c.execute(stmt)


def insert_many(con, sql: str, rows: list[tuple], chunk: int = 5000):
    """分批 executemany；rows 为与 sql 占位符顺序一致的元组列表。"""
    if not rows:
        return 0
    with con.cursor() as c:
        for i in range(0, len(rows), chunk):
            c.executemany(sql, rows[i:i + chunk])
    return len(rows)


def init_mysql(con) -> None:
    run_sql_file(con, DDL_PATH)
    cfg = load_config()
    load_dims(con, cfg)
    init_day_seq(con, cfg)


def init_day_seq(con, cfg) -> None:
    """日历表：store_day -> 次日/第7天/第30天（int 日期跨月运算的正规解法）。"""
    lo, hi = cfg.start, cfg.end

    def _sd(d: date) -> int:
        return int(d.strftime("%Y%m%d"))

    rows = []
    d = lo
    while d <= hi:
        rows.append((
            _sd(d),
            _sd(d + timedelta(days=1)) if d + timedelta(days=1) <= hi else None,
            _sd(d + timedelta(days=7)) if d + timedelta(days=7) <= hi else None,
            _sd(d + timedelta(days=30)) if d + timedelta(days=30) <= hi else None,
        ))
        d += timedelta(days=1)
    with con.cursor() as c:
        c.execute("DELETE FROM db_meta.t_day_seq")
    insert_many(con,
                "INSERT INTO db_meta.t_day_seq (store_day, next_day, day7, day30) VALUES (%s,%s,%s,%s)",
                rows)


def load_dims(con, cfg) -> None:
    def _replace(table: str, cols: str, rows: list[tuple]):
        with con.cursor() as c:
            c.execute(f"DELETE FROM {table}")
        placeholders = ",".join(["%s"] * len(cols.split(",")))
        insert_many(con, f"INSERT INTO {table} ({cols}) VALUES ({placeholders})", rows)

    _replace("db_adm.t_sy_game", "gid, game_name, pgid, launch_date, status", [
        (1, cfg.game_name, 5714, date.fromisoformat(cfg.raw["meta"]["launch_date"]), "online")])

    _replace("db_adm.t_sy_channel", "cid, channel_name, channel_type",
             [(c["id"], c["name"], c["type"]) for c in cfg.channels])

    merge_map = {}
    for m in cfg.merges:
        for sid in m["servers"]:
            merge_map[sid] = (m["group"], m["date"])
    _replace("db_adm.t_sy_server", "server_id, server_name, gid, open_date, merge_group, merge_date", [
        (s["id"], s["name"], 1, date.fromisoformat(s["open"]),
         merge_map.get(s["id"], (None, None))[0], merge_map.get(s["id"], (None, None))[1])
        for s in cfg.servers])

    _replace("db_adm.t_sy_item", "item_id, item_name, item_type",
             [(i["id"], i["name"], i["type"]) for i in cfg.raw["items"]])

    _replace("db_adm.t_sy_product", "product_id, product_name, price, product_type",
             [(p["id"], p["name"], float(p["price"]), p["type"]) for p in cfg.products])

    _replace("db_adm.t_sy_activity", "activity_id, activity_name, start_date, end_date, activity_type",
             [(a["id"], a["name"], date.fromisoformat(a["start"]),
               date.fromisoformat(a["end"]), a["type"]) for a in cfg.activities])

    notes = {v["version"]: v["note"] for v in cfg.raw["versions"]}
    _replace("db_adm.t_sy_version", "app_version, release_date, release_note",
             [(v, date.fromisoformat(str(d)), notes[v]) for d, v in cfg.versions])


def log_etl(con, step: str, store_day, elapsed_ms: int, rows: int = -1):
    with con.cursor() as c:
        c.execute("INSERT INTO db_meta.t_etl_log (run_time, step, store_day, elapsed_ms, rows_affected) "
                  "VALUES (NOW(), %s, %s, %s, %s)", [step, store_day, elapsed_ms, rows])


# --------------------------------------------------------- Elasticsearch 初始化

# 埋点明细索引模板：按月索引 mlog-{事件}-{YYYYMM}（按天过滤靠 store_day 字段），副本 0（单机环境）
ES_MAPPINGS = {
    "login": {"store_day": "integer", "log_time": "date", "session_id": "long",
              "account_id": "long", "role_id": "long", "server_id": "integer",
              "pid": "integer", "gid": "integer", "cid": "integer",
              "dev": "keyword", "os": "keyword", "app_version": "keyword", "status": "integer"},
    "logout": {"store_day": "integer", "session_id": "long", "logout_time": "date",
               "duration_seconds": "integer", "role_id": "long", "server_id": "integer",
               "cid": "integer"},
    "property": {"store_day": "integer", "flow_time": "date", "role_id": "long",
                 "server_id": "integer", "cid": "integer", "item_id": "integer",
                 "change_type": "keyword", "change_num": "integer", "balance_after": "long"},
    "ad": {"store_day": "integer", "watch_time": "date", "role_id": "long",
           "server_id": "integer", "cid": "integer", "ad_slot": "keyword",
           "reward_item_id": "integer"},
}


def init_es(es: Elasticsearch) -> None:
    for event, fields in ES_MAPPINGS.items():
        es.indices.put_index_template(
            name=f"mlog-{event}",
            index_patterns=[f"mlog-{event}-*"],
            template={
                "settings": {"number_of_shards": 1, "number_of_replicas": 0},
                "mappings": {"properties": {k: {"type": v} for k, v in fields.items()}},
            },
        )


def wait_es(es: Elasticsearch, timeout_s: int = 60) -> bool:
    import time
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if es.ping():
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def utf8_sig_csv(df: pd.DataFrame, path: Path):
    """导出给 Excel 打开的 CSV：utf-8-sig 带 BOM（真实公司交付物同款），Windows 友好。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig")
