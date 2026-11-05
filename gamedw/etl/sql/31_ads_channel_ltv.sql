-- ADS 渠道周cohort 质量表（全量重算）：买量评估视角，看各渠道 LTV 与留存（MySQL 方言）
DROP TABLE IF EXISTS db_dw_ads.t_channel_ltv_wk;

CREATE TABLE db_dw_ads.t_channel_ltv_wk AS
WITH reg AS (
    SELECT r.role_id,
           r.cid,
           DATE(DATE_SUB(r.create_time, INTERVAL WEEKDAY(r.create_time) DAY)) AS cohort_week
    FROM db_adm.t_sy_role r
    WHERE r.cid IN (SELECT cid FROM db_adm.t_sy_channel WHERE channel_type <> '测试')
),
cohort AS (
    SELECT cohort_week, cid, COUNT(*) AS reg_users
    FROM reg
    GROUP BY cohort_week, cid
),
act AS (
    SELECT g.cohort_week, g.cid, a.store_day, a.role_id, a.pay_amount, a.pay_cnt
    FROM reg g
    JOIN db_dw_dws.t_role_active_di a ON a.role_id = g.role_id
),
stat AS (
    SELECT cohort_week, cid,
           COUNT(DISTINCT CASE WHEN STR_TO_DATE(store_day, '%Y%m%d') = cohort_week + INTERVAL 7 DAY
                               THEN role_id END)                                            AS ret_w1_num,
           COUNT(DISTINCT CASE WHEN STR_TO_DATE(store_day, '%Y%m%d') = cohort_week + INTERVAL 28 DAY
                               THEN role_id END)                                            AS ret_w4_num,
           SUM(CASE WHEN STR_TO_DATE(store_day, '%Y%m%d') < cohort_week + INTERVAL 7 DAY
                    THEN pay_amount ELSE 0 END)                                             AS rev_7,
           SUM(CASE WHEN STR_TO_DATE(store_day, '%Y%m%d') < cohort_week + INTERVAL 30 DAY
                    THEN pay_amount ELSE 0 END)                                             AS rev_30,
           COUNT(DISTINCT CASE WHEN STR_TO_DATE(store_day, '%Y%m%d') < cohort_week + INTERVAL 30 DAY
                                    AND pay_cnt > 0 THEN role_id END)                       AS payers_30
    FROM act
    GROUP BY cohort_week, cid
)
SELECT c.cohort_week,
       c.cid,
       ch.channel_name,
       c.reg_users,
       CASE WHEN c.cohort_week + INTERVAL 7 DAY <=
                 (SELECT STR_TO_DATE(MAX(store_day), '%Y%m%d') FROM db_dw_dws.t_role_active_di)
            THEN s.ret_w1_num / c.reg_users END AS ret_w1,
       CASE WHEN c.cohort_week + INTERVAL 28 DAY <=
                 (SELECT STR_TO_DATE(MAX(store_day), '%Y%m%d') FROM db_dw_dws.t_role_active_di)
            THEN s.ret_w4_num / c.reg_users END AS ret_w4,
       CASE WHEN c.cohort_week + INTERVAL 7 DAY <=
                 (SELECT STR_TO_DATE(MAX(store_day), '%Y%m%d') FROM db_dw_dws.t_role_active_di)
            THEN s.rev_7 / c.reg_users END      AS ltv_7,
       CASE WHEN c.cohort_week + INTERVAL 30 DAY <=
                 (SELECT STR_TO_DATE(MAX(store_day), '%Y%m%d') FROM db_dw_dws.t_role_active_di)
            THEN s.rev_30 / c.reg_users END     AS ltv_30,
       CAST(COALESCE(s.payers_30, 0) AS SIGNED) AS pay_users_30,
       CASE WHEN c.cohort_week + INTERVAL 30 DAY <=
                 (SELECT STR_TO_DATE(MAX(store_day), '%Y%m%d') FROM db_dw_dws.t_role_active_di)
            THEN COALESCE(s.payers_30, 0) / c.reg_users END AS pay_rate_30
FROM cohort c
LEFT JOIN stat s ON s.cohort_week = c.cohort_week AND s.cid = c.cid
JOIN db_adm.t_sy_channel ch ON ch.cid = c.cid
ORDER BY c.cohort_week, c.cid;
