"""复杂指标模板（对齐真实 nirvana-web 的 sql_template + VariablePO）：口径固定、SQL 固定、只填参数。
留存类指标无法用单表 DSL 表达——这是"DSL + 模板"双轨存在的根本原因。
"""
from queryagent.knowledge.registry import Registry


def retention_d1(registry: Registry, start_sd: int, end_sd: int) -> str:
    """新增次日留存（点值口径，按日输出）：新增数 / D+1 回访数。"""
    return f"""
SELECT a.store_day                                                   AS store_day,
       COUNT(DISTINCT a.role_id)                                     AS new_users,
       COUNT(DISTINCT CASE WHEN b.role_id IS NOT NULL THEN a.role_id END) AS retained_users,
       ROUND(COUNT(DISTINCT CASE WHEN b.role_id IS NOT NULL THEN a.role_id END)
             / COUNT(DISTINCT a.role_id), 4)                         AS ret_new_d1
FROM db_dw_dws.t_role_active_di a
LEFT JOIN db_meta.t_day_seq ds ON ds.store_day = a.store_day
LEFT JOIN db_dw_dws.t_role_active_di b
       ON b.role_id = a.role_id AND b.store_day = ds.next_day
WHERE a.is_new_role = 1
  AND a.store_day BETWEEN {start_sd} AND {end_sd}
GROUP BY a.store_day
ORDER BY a.store_day
""".strip()


def channel_ltv(registry: Registry) -> str:
    """渠道 30 日 LTV 与留存（成熟 cohort），读 ADS 周cohort 表。"""
    return """
SELECT ch.channel_name,
       SUM(c.reg_users)                                          AS reg_users,
       ROUND(AVG(c.ret_w1) * 100, 1)                             AS ret_w1_pct,
       ROUND(AVG(c.ltv_7), 2)                                    AS ltv_7,
       ROUND(AVG(c.ltv_30), 2)                                   AS ltv_30
FROM db_dw_ads.t_channel_ltv_wk c
JOIN db_adm.t_sy_channel ch ON ch.cid = c.cid
WHERE c.ltv_30 IS NOT NULL
GROUP BY ch.channel_name
ORDER BY ltv_30 DESC
""".strip()


def build_template(template_id: str, registry: Registry, time_range=None) -> str:
    if template_id == "retention_d1":
        s, e = time_range or (0, 0)
        return retention_d1(registry, s, e)
    if template_id == "channel_ltv":
        return channel_ltv(registry)
    raise ValueError(f"未知模板 {template_id}")
