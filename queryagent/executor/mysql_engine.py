"""MySQL 只读执行器：护栏 = 仅 SELECT + 超时 + 行数上限。"""
import time

import pandas as pd

from queryagent.config import SETTINGS
from gamedw.db import mysql_connect

_ALLOWED_PREFIX = ("SELECT", "SHOW", "DESC", "EXPLAIN")


def execute(sql: str) -> tuple[pd.DataFrame, int, bool]:
    """执行只读 SQL，返回 (df, 耗时ms, 是否被行数上限截断)。"""
    head = sql.strip().split("\n")[0].strip().upper()
    if not head.startswith(_ALLOWED_PREFIX):
        raise PermissionError("仅允许只读查询（SELECT/SHOW/DESC/EXPLAIN）")
    con = mysql_connect()
    try:
        with con.cursor() as c:
            c.execute(f"SET SESSION MAX_EXECUTION_TIME={SETTINGS.query_timeout_ms}")
        t0 = time.time()
        with con.cursor() as c:
            c.execute(sql)
            cols = [d[0] for d in c.description]
            rows = c.fetchmany(SETTINGS.row_cap + 1)
        elapsed = int((time.time() - t0) * 1000)
        truncated = len(rows) > SETTINGS.row_cap
        rows = rows[:SETTINGS.row_cap]
        return pd.DataFrame(rows, columns=cols), elapsed, truncated
    finally:
        con.close()
