-- 用户/角色维表（全量重刷）：从业务库快照清洗，剔除测试/冻结账号（MySQL 方言）
DELETE FROM db_adm.t_sy_account;

INSERT INTO db_adm.t_sy_account
SELECT a.account_id,
       a.register_time,
       a.pid,
       a.cid,
       a.status
FROM db_mlog.t_sync_account a
JOIN db_adm.t_sy_channel c ON c.cid = a.cid AND c.channel_type <> '测试';

DELETE FROM db_adm.t_sy_role;

INSERT INTO db_adm.t_sy_role
SELECT r.role_id,
       r.account_id,
       r.role_name,
       r.server_id,
       a.cid,
       a.pid,
       r.gid,
       r.create_time,
       r.last_login_time,
       r.level
FROM db_mlog.t_sync_role r
JOIN db_mlog.t_sync_account a ON a.account_id = r.account_id
JOIN db_adm.t_sy_channel c ON c.cid = a.cid AND c.channel_type <> '测试';
