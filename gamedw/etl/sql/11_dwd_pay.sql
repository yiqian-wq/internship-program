-- DWD 支付事实（全量重刷，订单量小）：按 order_id 去重、剔除测试渠道、打首充/退款标记（MySQL 方言）
DELETE FROM db_dw_dwd.t_pay_di;

INSERT INTO db_dw_dwd.t_pay_di
WITH dedup AS (
    SELECT o.*,
           ROW_NUMBER() OVER (PARTITION BY o.order_id ORDER BY o._sync_time) AS rn
    FROM db_mlog.t_sdk_pay_order o
    WHERE o.cid IN (SELECT cid FROM db_adm.t_sy_channel WHERE channel_type <> '测试')
),
first_pay AS (
    SELECT role_id, MIN(pay_time) AS first_time
    FROM dedup
    WHERE rn = 1
    GROUP BY role_id
)
SELECT d.store_day,
       d.order_id,
       d.pay_time,
       d.account_id,
       d.role_id,
       d.server_id,
       d.pid,
       d.gid,
       d.cid,
       c.channel_name,
       d.product_id,
       p.product_name,
       p.product_type,
       p.price AS standard_price,
       d.amount,
       d.status,
       d.status = 'refunded'     AS is_refund,
       d.pay_time = f.first_time AS is_first_pay
FROM dedup d
JOIN db_adm.t_sy_channel c ON c.cid = d.cid
JOIN db_adm.t_sy_product p ON p.product_id = d.product_id
LEFT JOIN first_pay f ON f.role_id = d.role_id
WHERE d.rn = 1;
