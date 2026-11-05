-- ADS 游戏核心指标日报（全量重算：留存类指标依赖后置数据，小数仓惯用全量重刷）
-- MySQL 方言：FILTER 改 SUM(CASE WHEN)；int 日期跨月运算改查 db_meta.t_day_seq 日历表
DROP TABLE IF EXISTS db_dw_ads.t_game_overview_di;

CREATE TABLE db_dw_ads.t_game_overview_di AS
WITH act AS (
    SELECT store_day, role_id, is_new_role, login_cnt, online_minutes,
           pay_cnt, pay_amount, is_first_pay_day, ad_watch_cnt, gacha_cnt
    FROM db_dw_dws.t_role_active_di
),
daily AS (
    SELECT store_day,
           COUNT(*) AS dau,
           SUM(CASE WHEN is_new_role THEN 1 ELSE 0 END) AS new_users,
           SUM(login_cnt) AS session_cnt,
           SUM(online_minutes) AS online_minutes,
           SUM(pay_amount) AS revenue,
           SUM(CASE WHEN pay_cnt > 0 THEN 1 ELSE 0 END) AS pay_users,
           SUM(CASE WHEN is_first_pay_day THEN 1 ELSE 0 END) AS first_pay_users,
           SUM(ad_watch_cnt) AS ad_watch_cnt,
           SUM(CASE WHEN ad_watch_cnt > 0 THEN 1 ELSE 0 END) AS ad_watch_users,
           SUM(gacha_cnt) AS gacha_cnt,
           SUM(CASE WHEN gacha_cnt > 0 THEN 1 ELSE 0 END) AS gacha_users
    FROM act
    GROUP BY store_day
),
-- 全量 DAU 次日回访：next_day 来自日历表，规避 int 日期跨月加法错误
ret_dau AS (
    SELECT a.store_day AS store_day, COUNT(*) AS dau_ret_d1
    FROM act a
    JOIN db_meta.t_day_seq ds ON ds.store_day = a.store_day
    JOIN act b ON b.role_id = a.role_id AND b.store_day = ds.next_day
    GROUP BY a.store_day
),
-- 新增用户 cohort 的 d1 / d7 留存（点值口径）：a 侧=注册当日，b 侧=其后任意活跃日
nact AS (
    SELECT store_day, role_id FROM act WHERE is_new_role
),
ret_new AS (
    SELECT a.store_day AS store_day,
           SUM(CASE WHEN b.store_day = ds.next_day THEN 1 ELSE 0 END) AS ret_d1,
           SUM(CASE WHEN b.store_day = ds.day7 THEN 1 ELSE 0 END) AS ret_d7
    FROM nact a
    JOIN db_meta.t_day_seq ds ON ds.store_day = a.store_day
    JOIN act b ON b.role_id = a.role_id AND b.store_day IN (ds.next_day, ds.day7)
    GROUP BY a.store_day
),
ref AS (
    SELECT store_day, SUM(amount) AS refund_amount
    FROM db_dw_dwd.t_pay_di
    WHERE is_refund
    GROUP BY store_day
)
SELECT d.store_day,
       d.dau,
       d.new_users,
       d.new_users / d.dau                                                AS new_ratio,
       CASE WHEN ds.next_day IS NOT NULL                                  -- 留存窗口已成熟
            THEN n.ret_d1 / NULLIF(d.new_users, 0) END                    AS ret_new_d1,
       CASE WHEN ds.day7 IS NOT NULL
            THEN n.ret_d7 / NULLIF(d.new_users, 0) END                    AS ret_new_d7,
       CASE WHEN ds.next_day IS NOT NULL
            THEN r.dau_ret_d1 / d.dau END                                 AS dau_ret_d1,
       d.pay_users,
       d.pay_users / d.dau                                                AS pay_rate,
       d.revenue                                                          AS revenue,
       COALESCE(f.refund_amount, 0)                                       AS refund_amount,
       d.revenue / d.dau                                                  AS arpu,
       d.revenue / NULLIF(d.pay_users, 0)                                 AS arppu,
       d.first_pay_users,
       d.session_cnt,
       d.online_minutes,
       d.online_minutes / d.dau                                           AS online_avg_min,
       d.ad_watch_users,
       d.ad_watch_cnt,
       d.gacha_users,
       d.gacha_cnt
FROM daily d
LEFT JOIN ret_new n ON n.store_day = d.store_day
LEFT JOIN ret_dau r ON r.store_day = d.store_day
LEFT JOIN ref f ON f.store_day = d.store_day
LEFT JOIN db_meta.t_day_seq ds ON ds.store_day = d.store_day
ORDER BY d.store_day;
