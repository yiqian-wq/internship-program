-- DWS 活跃角色日汇总（按天幂等）：一日一角色一行，是数仓核心工作表
DELETE FROM db_dw_dws.t_role_active_di WHERE store_day = ${dt};

INSERT INTO db_dw_dws.t_role_active_di
WITH login_agg AS (
    SELECT role_id,
           COUNT(*) AS login_cnt,
           CAST(SUM(duration_seconds) // 60 AS INTEGER) AS online_minutes,
           MAX(login_time) AS last_login_time
    FROM db_dw_dwd.t_login_di
    WHERE store_day = ${dt}
    GROUP BY role_id
),
pay_agg AS (
    SELECT role_id,
           COUNT(*) AS pay_cnt,
           SUM(amount) AS pay_amount,
           BOOL_OR(is_first_pay) AS is_first_pay_day
    FROM db_dw_dwd.t_pay_di
    WHERE store_day = ${dt} AND status = 'paid'
    GROUP BY role_id
),
ad_agg AS (
    SELECT role_id, COUNT(*) AS ad_watch_cnt
    FROM db_dw_dwd.t_ad_watch_di
    WHERE store_day = ${dt}
    GROUP BY role_id
),
gacha_agg AS (
    SELECT role_id, CAST(SUM(change_num) AS INTEGER) AS gacha_cnt
    FROM db_dw_dwd.t_property_flow_di
    WHERE store_day = ${dt} AND change_type = 'gacha_consume'
    GROUP BY role_id
)
SELECT ${dt}                                          AS store_day,
       l.role_id,
       r.account_id,
       r.server_id,
       s.merge_group,
       r.cid,
       r.pid,
       r.gid,
       CAST(r.create_time AS DATE) = sd_to_date(${dt}) AS is_new_role,
       l.login_cnt,
       l.online_minutes,
       l.last_login_time,
       COALESCE(p.pay_cnt, 0)                         AS pay_cnt,
       COALESCE(p.pay_amount, 0)                      AS pay_amount,
       COALESCE(p.is_first_pay_day, FALSE)            AS is_first_pay_day,
       COALESCE(a.ad_watch_cnt, 0)                    AS ad_watch_cnt,
       COALESCE(g.gacha_cnt, 0)                       AS gacha_cnt
FROM login_agg l
JOIN db_adm.t_sy_role r ON r.role_id = l.role_id
LEFT JOIN db_adm.t_sy_server s ON s.server_id = r.server_id
LEFT JOIN pay_agg p ON p.role_id = l.role_id
LEFT JOIN ad_agg a ON a.role_id = l.role_id
LEFT JOIN gacha_agg g ON g.role_id = l.role_id;
