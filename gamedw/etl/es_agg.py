"""ETL：从 Elasticsearch 聚合当日埋点 → db_dw_dws.t_role_active_di（按天幂等）。

架构说明：埋点明细只存 ES（mlog-* 按天索引），数仓的"角色日汇总"由本步骤
用 ES terms/sub-aggregations 加工后写入 MySQL——这是小公司 "ES 明细 + MySQL 分层" 的典型链路。
"""
from datetime import datetime

from elasticsearch import Elasticsearch


def _terms_agg(es: Elasticsearch, event: str, yyyymm: str, store_day: int, agg_body: dict) -> dict:
    """在月索引上按 store_day 过滤，按 role_id 聚合，返回 {role_id: bucket}。"""
    body = {"size": 0,
            "query": {"term": {"store_day": store_day}},
            "aggs": {"by_role": agg_body}}
    res = es.search(index=f"mlog-{event}-{yyyymm}", body=body)
    buckets = res["aggregations"]["by_role"]["buckets"]
    return {b["key"]: b for b in buckets}


def run(con, es: Elasticsearch, store_day: int) -> int:
    yyyymm = str(store_day)[:6]
    if not es.indices.exists(index=f"mlog-login-{yyyymm}"):
        return 0          # 当月无埋点数据（未生成/未采集），幂等空跑

    # 登录次数 + 最后登录时间（login 索引）
    login = _terms_agg(es, "login", yyyymm, store_day, {
        "terms": {"field": "role_id", "size": 65535},
        "aggs": {"last_login": {"max": {"field": "log_time"}}},
    })
    # 在线时长（logout 索引，sum duration_seconds）
    logout = _terms_agg(es, "logout", yyyymm, store_day, {
        "terms": {"field": "role_id", "size": 65535},
        "aggs": {"dur": {"sum": {"field": "duration_seconds"}}},
    })
    # 抽卡次数（property 索引，过滤 gacha_consume 后 sum change_num）
    gacha = _terms_agg(es, "property", yyyymm, store_day, {
        "terms": {"field": "role_id", "size": 65535},
        "aggs": {"g": {"filter": {"term": {"change_type": "gacha_consume"}},
                        "aggs": {"n": {"sum": {"field": "change_num"}}}}},
    })
    # 广告观看次数（ad 索引）
    ad = _terms_agg(es, "ad", yyyymm, store_day, {
        "terms": {"field": "role_id", "size": 65535},
    })

    role_ids = set(login) | set(logout) | set(gacha) | set(ad)
    if not role_ids:
        return 0

    # 角色维度（t_sy_role 已剔除测试账号）与合服信息
    dims = {}
    ids = list(role_ids)
    with con.cursor() as c:
        for i in range(0, len(ids), 1000):
            chunk = ids[i:i + 1000]
            placeholders = ",".join(["%s"] * len(chunk))
            c.execute(f"SELECT role_id, account_id, server_id, cid, pid, gid, create_time "
                      f"FROM db_adm.t_sy_role WHERE role_id IN ({placeholders})", chunk)
            for r in c.fetchall():
                dims[r[0]] = r
        c.execute("SELECT server_id, merge_group FROM db_adm.t_sy_server")
        merges = {r[0]: r[1] for r in c.fetchall()}

    # 当日付费（MySQL）
    with con.cursor() as c:
        c.execute("SELECT role_id, COUNT(*), SUM(amount), MAX(is_first_pay) "
                  "FROM db_dw_dwd.t_pay_di WHERE store_day = %s AND status = 'paid' "
                  "GROUP BY role_id", [store_day])
        pay = {r[0]: r for r in c.fetchall()}

    rows = []
    for rid in role_ids:
        dim = dims.get(rid)
        if dim is None:      # 测试账号/未清洗角色：不入库
            continue
        account_id, server_id, cid, pid, gid, create_time = dim[1], dim[2], dim[3], dim[4], dim[5], dim[6]
        login_cnt = int(login.get(rid, {}).get("doc_count", 0))
        online_minutes = int(logout.get(rid, {}).get("dur", {}).get("value", 0) or 0) // 60
        g = gacha.get(rid, {}).get("g", {})
        gacha_cnt = int(g.get("n", {}).get("value", 0) or 0)
        ad_watch_cnt = int(ad.get(rid, {}).get("doc_count", 0))
        p = pay.get(rid, (rid, 0, 0, 0))   # 行结构: (role_id, pay_cnt, pay_amount, is_first_pay)
        is_new = 1 if create_time is not None and \
            int(create_time.strftime("%Y%m%d")) == store_day else 0
        last_login = login.get(rid, {}).get("last_login", {}).get("value")
        last_login_str = datetime.fromtimestamp(last_login / 1000).strftime("%Y-%m-%d %H:%M:%S") \
            if last_login else None
        rows.append((store_day, rid, account_id, server_id, merges.get(server_id),
                     cid, pid, gid, is_new, login_cnt, online_minutes, last_login_str,
                     int(p[1]), float(p[2] or 0), int(p[3] or 0), ad_watch_cnt, gacha_cnt))

    with con.cursor() as c:
        c.execute("DELETE FROM db_dw_dws.t_role_active_di WHERE store_day = %s", [store_day])
    from gamedw.db import insert_many
    return insert_many(
        con,
        "INSERT INTO db_dw_dws.t_role_active_di (store_day, role_id, account_id, server_id, "
        "merge_group, cid, pid, gid, is_new_role, login_cnt, online_minutes, last_login_time, "
        "pay_cnt, pay_amount, is_first_pay_day, ad_watch_cnt, gacha_cnt) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        rows)
