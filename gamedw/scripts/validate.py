"""数据质检（MySQL + Elasticsearch 双引擎）：趋势、口径、脏数据清洗效果。

用法：python -m gamedw.scripts.validate
"""
from gamedw.db import _utf8_console, es_client, mysql_connect

MYSQL_TABLES = [
    ("db_mlog", "t_sdk_pay_order"), ("db_mlog", "t_sync_account"), ("db_mlog", "t_sync_role"),
    ("db_adm", "t_sy_role"), ("db_dw_dwd", "t_pay_di"), ("db_dw_dws", "t_role_active_di"),
    ("db_dw_dws", "t_server_channel_di"), ("db_dw_ads", "t_game_overview_di"),
    ("db_dw_ads", "t_channel_ltv_wk"),
]
ES_EVENTS = ["login", "logout", "property", "ad"]

ok_count = warn_count = 0


def check(name: str, cond: bool, detail: str):
    global ok_count, warn_count
    mark = "PASS" if cond else "WARN"
    if not cond:
        warn_count += 1
    else:
        ok_count += 1
    print(f"  [{mark}] {name}: {detail}")


def main():
    _utf8_console()
    con = mysql_connect()
    es = es_client()

    print("=" * 72)
    print("一、各层表行数（MySQL + ES）")
    print("=" * 72)
    total = 0
    for db, t in MYSQL_TABLES:
        with con.cursor() as c:
            c.execute(f"SELECT COUNT(*) FROM {db}.{t}")
            n = c.fetchone()[0]
        total += n
        print(f"  {db}.{t:<26} {n:>12,}")
    for ev in ES_EVENTS:
        res = es.count(index=f"mlog-{ev}-*")
        total += res["count"]
        print(f"  {'ES mlog-' + ev + '-*':<34} {res['count']:>12,}")
    print(f"  {'合计':<34} {total:>12,}")

    print("=" * 72)
    print("二、核心趋势（t_game_overview_di 按月汇总）")
    print("=" * 72)
    with con.cursor() as c:
        c.execute("""
            SELECT store_day DIV 100 AS m, COUNT(*) AS days,
                   CAST(AVG(dau) AS SIGNED) AS avg_dau, MAX(dau) AS max_dau,
                   ROUND(AVG(ret_new_d1), 3) AS avg_ret_d1,
                   CAST(SUM(revenue) AS SIGNED) AS revenue
            FROM db_dw_ads.t_game_overview_di GROUP BY store_day DIV 100 ORDER BY 1
        """)
        rows = c.fetchall()
    print(f"  {'月份':<10}{'天数':>5}{'日均DAU':>9}{'峰值DAU':>9}{'新增次留':>9}{'月流水(元)':>14}")
    for m, days, dau, mx, ret, rev in rows:
        print(f"  {m:<10}{days:>5}{dau:>9,}{mx:>9,}{ret:>9}{rev:>14,}")

    print("=" * 72)
    print("三、业务剧本埋点验证")
    print("=" * 72)
    with con.cursor() as c:
        c.execute("SELECT MAX(dau) FROM db_dw_ads.t_game_overview_di WHERE store_day BETWEEN 20260214 AND 20260302")
        spring = c.fetchone()[0]
        c.execute("SELECT CAST(AVG(dau) AS SIGNED) FROM db_dw_ads.t_game_overview_di WHERE store_day BETWEEN 20260310 AND 20260405")
        base_dau = c.fetchone()[0]
    if spring is None or base_dau is None:
        check("春节活动抬升DAU", False, "数据未覆盖春节窗口（冒烟模式跳过）")
    else:
        check("春节活动抬升DAU", spring > base_dau * 1.3, f"春节峰值 {spring:,} vs 节后均值 {base_dau:,}")

    def avg_ret(a, b):
        with con.cursor() as c:
            c.execute("SELECT AVG(ret_new_d1) FROM db_dw_ads.t_game_overview_di WHERE store_day BETWEEN %s AND %s", [a, b])
            v = c.fetchone()[0]
            return float(v) if v is not None else None
    before, during, after = avg_ret(20260908, 20260921), avg_ret(20260924, 20261001), avg_ret(20261004, 20261014)
    if before is None or during is None or after is None:
        check("v2.1事故期新增次留显著下滑", False, "数据未覆盖事故窗口（冒烟模式跳过）")
        check("修复后次留恢复", False, "数据未覆盖事故窗口（冒烟模式跳过）")
    else:
        check("v2.1事故期新增次留显著下滑", during < before - 0.04, f"事故前 {before:.3f} → 事故中 {during:.3f}")
        check("修复后次留恢复", after > during + 0.03, f"修复后 {after:.3f}")

    with con.cursor() as c:
        c.execute("""SELECT AVG(CASE WHEN channel_name = 'TapTap' THEN ltv_30 END),
                            AVG(CASE WHEN channel_name = '抖音买量' THEN ltv_30 END)
                     FROM db_dw_ads.t_channel_ltv_wk WHERE reg_users > 200""")
        tap, dy = c.fetchone()
    if tap is None or dy is None:
        check("TapTap 30日LTV 高于抖音买量", False, "LTV cohort 未成熟（冒烟模式跳过）")
    else:
        check("TapTap 30日LTV 高于抖音买量", tap > dy, f"TapTap {tap:.2f} vs 抖音 {dy:.2f}")

    print("=" * 72)
    print("四、脏数据与清洗效果")
    print("=" * 72)
    with con.cursor() as c:
        c.execute("""SELECT COUNT(*) FROM (
                        SELECT order_id FROM db_mlog.t_sdk_pay_order GROUP BY order_id HAVING COUNT(*) > 1) x""")
        dup = c.fetchone()[0]
        c.execute("""SELECT COUNT(*) FROM (
                        SELECT order_id FROM db_dw_dwd.t_pay_di GROUP BY order_id HAVING COUNT(*) > 1) x""")
        dup_dwd = c.fetchone()[0]
    check("ODS存在同步重复订单（6月中 glitch）", dup > 0, f"重复 order_id {dup} 个")
    check("DWD 已去重干净", dup_dwd == 0, f"DWD 重复 order_id {dup_dwd} 个")

    # ES 侧：缺失渠道 + 测试渠道（埋点明细存 ES）
    miss = es.count(index="mlog-login-*", query={"term": {"cid": -1}})["count"]
    test_docs = es.count(index="mlog-login-*", query={"term": {"cid": 9999}})["count"]
    check("ES 埋点存在缺失渠道行", miss > 0, f"cid=-1 共 {miss:,} 行")
    check("ES 埋点保留测试账号（ODS 语义）", test_docs > 0, f"测试渠道登录 {test_docs:,} 行")

    with con.cursor() as c:
        c.execute("SELECT COUNT(*) FROM db_dw_dws.t_role_active_di r "
                  "JOIN db_adm.t_sy_channel ch ON ch.cid = r.cid WHERE ch.channel_type = '测试'")
        test_in_dws = c.fetchone()[0]
    check("DWS 不含测试渠道数据", test_in_dws == 0, "维表清洗已剔除")

    with con.cursor() as c:
        c.execute("SELECT COUNT(*) FROM db_mlog.t_sdk_pay_order WHERE amount > 10000")
        outlier = c.fetchone()[0]
        c.execute("SELECT COUNT(*), CAST(SUM(amount) AS SIGNED) FROM db_dw_dwd.t_pay_di WHERE is_refund")
        refund = c.fetchone()
    check("ODS 保留异常大额订单（语义层处理素材）", outlier >= 3, f"共 {outlier} 笔")
    check("退款订单已标记", refund[0] > 0, f"{refund[0]:,} 笔 / {refund[1]:,} 元")

    # ES 明细检索能力演示（阶段1 课程素材：查某玩家登录明细）
    with con.cursor() as c:
        c.execute("SELECT role_id FROM db_adm.t_sy_role ORDER BY role_id LIMIT 1")
        sample_role = c.fetchone()[0]
    res = es.search(index="mlog-login-*", size=3, query={"term": {"role_id": sample_role}})
    check("ES 明细检索可用（按 role_id 查登录日志）", res["hits"]["total"]["value"] > 0,
          f"示例角色 {sample_role} 命中 {res['hits']['total']['value']:,} 条登录明细")

    print("=" * 72)
    print(f"质检完成：{ok_count} 项通过，{warn_count} 项警告")
    print("=" * 72)


if __name__ == "__main__":
    main()
