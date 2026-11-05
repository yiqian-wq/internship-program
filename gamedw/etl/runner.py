"""数仓跑批入口（模拟小公司 crontab 调度）：MySQL + ES 双引擎。

步骤分两类：
  SQL 步骤：执行 etl/sql/*.sql（${dt} 替换为 int 分区）
  Python 步骤：15_es_role_active（ES 聚合埋点 → DWS）

用法：
    python -m gamedw.etl.runner                 # 全流程
    python -m gamedw.etl.runner --dt 2026-09-25 # 只跑某天（日常增量 + 报表全量重算）
    python -m gamedw.etl.runner --step dws      # 只跑某层
    python -m gamedw.etl.runner --from 2026-09-01 --to 2026-09-30
"""
import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path

from gamedw.db import (_utf8_console, es_client, insert_many, log_etl,
                       mysql_connect, run_sql_file, wait_es)

SQL_DIR = Path(__file__).parent / "sql"

# 按天幂等的任务（T+1 调度，每天只处理前一天分区）
PY_DAY_STEPS = ["15_es_role_active"]                      # ES 聚合 → DWS
SQL_DAY_STEPS = ["21_dws_server_channel"]
# 全量重刷的任务（小表/依赖后置数据，小数仓常态）
SQL_FULL_STEPS = ["05_dim_role", "11_dwd_pay"]
LAYER_OF = {"dwd": ["05_dim_role", "11_dwd_pay"],
            "dws": ["15_es_role_active", "21_dws_server_channel"],
            "ads": ["30_ads_overview", "31_ads_channel_ltv"]}


def _sd_to_date(sd: int) -> date:
    return date(int(str(sd)[:4]), int(str(sd)[4:6]), int(str(sd)[6:8]))


def _date_to_sd(d: date) -> int:
    return int(d.strftime("%Y%m%d"))


def run_sql_step(con, name: str, store_day=None) -> int:
    t0 = time.time()
    sql = "\n".join(line for line in (SQL_DIR / f"{name}.sql").read_text(encoding="utf-8").splitlines()
                    if not line.strip().startswith("--"))
    if store_day:
        sql = sql.replace("${dt}", str(int(store_day)))
    rows = 0
    for stmt in (s.strip() for s in sql.split(";")):
        if not stmt:
            continue
        with con.cursor() as c:
            c.execute(stmt)
            if c.description:              # SELECT 有结果集
                rows += c.rowcount if c.rowcount and c.rowcount > 0 else 0
            else:
                rows += max(c.rowcount, 0)
    elapsed = int((time.time() - t0) * 1000)
    log_etl(con, name, store_day, elapsed, rows)
    print(f"[etl] {name:<24} {str(store_day or 'full'):<12} rows={rows:>10,}  {elapsed:>6}ms", flush=True)
    return rows


def run_es_role_active(con, es, store_day: int) -> int:
    from gamedw.etl.es_agg import run as es_agg_run
    t0 = time.time()
    rows = es_agg_run(con, es, store_day)
    elapsed = int((time.time() - t0) * 1000)
    log_etl(con, "15_es_role_active", store_day, elapsed, rows)
    print(f"[etl] {'15_es_role_active':<24} {store_day:<12} rows={rows:>10,}  {elapsed:>6}ms", flush=True)
    return rows


def day_range(con, from_d, to_d):
    """按 store_day(int YYYYMMDD) 迭代分区（取自 ES 埋点覆盖范围）。"""
    # ES 覆盖范围即埋点范围；MySQL 侧用 t_day_seq 判断边界
    with con.cursor() as c:
        c.execute("SELECT MIN(store_day), MAX(store_day) FROM db_meta.t_day_seq")
        lo, hi = c.fetchone()
    lo_d = from_d or _sd_to_date(lo)
    hi_d = to_d or _sd_to_date(hi)
    d = lo_d
    while d <= hi_d:
        yield _date_to_sd(d)
        d += timedelta(days=1)


def main():
    _utf8_console()
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", choices=["all", "dwd", "dws", "ads"], default="all")
    ap.add_argument("--dt", type=date.fromisoformat, help="只跑某个分区日（含当天 dwd/dws + 全量 ads/dim）")
    ap.add_argument("--from", dest="from_d", type=date.fromisoformat)
    ap.add_argument("--to", dest="to_d", type=date.fromisoformat)
    args = ap.parse_args()

    t0 = time.time()
    con = mysql_connect()
    from gamedw.db import init_mysql
    init_mysql(con)
    es = es_client()
    if not wait_es(es):
        raise SystemExit("[etl] Elasticsearch 连接失败")

    print(f"[etl] 开始跑批 step={args.step}" + (f" dt={args.dt}" if args.dt else ""), flush=True)
    dt_sd = _date_to_sd(args.dt) if args.dt else None

    if dt_sd:  # 单日模式：维表/订单全量重刷 + 当天增量 + 报表全量重算
        for name in SQL_FULL_STEPS:
            run_sql_step(con, name)
        run_es_role_active(con, es, dt_sd)
        for name in SQL_DAY_STEPS:
            run_sql_step(con, name, dt_sd)
        if args.step in ("all", "ads"):
            for name in LAYER_OF["ads"]:
                run_sql_step(con, name)
    else:
        if args.step in ("all", "dwd"):
            for name in SQL_FULL_STEPS:
                run_sql_step(con, name)
        if args.step in ("all", "dws"):
            for sd in day_range(con, args.from_d and _date_to_sd(args.from_d),
                                args.to_d and _date_to_sd(args.to_d)):
                run_es_role_active(con, es, sd)
                for name in SQL_DAY_STEPS:
                    run_sql_step(con, name, sd)
        if args.step in ("all", "ads"):
            for name in LAYER_OF["ads"]:
                run_sql_step(con, name)

    print(f"[etl] 跑批完成，总用时 {time.time() - t0:.1f}s", flush=True)


if __name__ == "__main__":
    main()
