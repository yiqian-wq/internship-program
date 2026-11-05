-- DWS 服×渠道日汇总（按天幂等，MySQL 方言）
DELETE FROM db_dw_dws.t_server_channel_di WHERE store_day = ${dt};

INSERT INTO db_dw_dws.t_server_channel_di
SELECT store_day,
       server_id,
       merge_group,
       cid,
       COUNT(*) AS dau,
       SUM(CASE WHEN is_new_role THEN 1 ELSE 0 END) AS new_roles,
       SUM(pay_amount) AS pay_amount,
       SUM(CASE WHEN pay_cnt > 0 THEN 1 ELSE 0 END) AS pay_users,
       SUM(online_minutes) AS online_minutes,
       SUM(ad_watch_cnt) AS ad_watch_cnt,
       SUM(gacha_cnt) AS gacha_cnt
FROM db_dw_dws.t_role_active_di
WHERE store_day = ${dt}
GROUP BY store_day, server_id, merge_group, cid;
