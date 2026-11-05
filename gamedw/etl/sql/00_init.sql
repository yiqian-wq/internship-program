-- 初始化（MySQL 版）：分层库、全部表结构、索引
-- 命名对齐公司真实规范：db_ 前缀库、t_ 前缀表、store_day int 分区（YYYYMMDD）
-- 分工：埋点明细存 Elasticsearch（mlog-* 索引），MySQL 存业务同步/维表/数仓分层
-- 幂等：可重复执行

CREATE DATABASE IF NOT EXISTS db_mlog DEFAULT CHARACTER SET utf8mb4;
CREATE DATABASE IF NOT EXISTS db_adm  DEFAULT CHARACTER SET utf8mb4;
CREATE DATABASE IF NOT EXISTS db_dw_dwd DEFAULT CHARACTER SET utf8mb4;
CREATE DATABASE IF NOT EXISTS db_dw_dws DEFAULT CHARACTER SET utf8mb4;
CREATE DATABASE IF NOT EXISTS db_dw_ads DEFAULT CHARACTER SET utf8mb4;
CREATE DATABASE IF NOT EXISTS db_meta DEFAULT CHARACTER SET utf8mb4;

-- ============ db_mlog：业务库同步落地（订单/快照） ============

CREATE TABLE IF NOT EXISTS db_mlog.t_sdk_pay_order (
    store_day   INT NOT NULL,
    order_id    VARCHAR(32) NOT NULL,
    pay_time    DATETIME,
    account_id  BIGINT,
    role_id     BIGINT,
    server_id   SMALLINT,
    pid         SMALLINT,
    gid         SMALLINT,
    cid         SMALLINT,
    product_id  VARCHAR(16),
    amount      DECIMAL(10,2),
    status      VARCHAR(16),
    _sync_time  DATETIME,
    KEY idx_order (order_id),
    KEY idx_day_cid (store_day, cid)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

CREATE TABLE IF NOT EXISTS db_mlog.t_sync_account (
    account_id    BIGINT PRIMARY KEY,
    register_time DATETIME,
    pid           SMALLINT,
    cid           SMALLINT,
    dev           VARCHAR(32),
    status        VARCHAR(16),
    _sync_time    DATETIME
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

CREATE TABLE IF NOT EXISTS db_mlog.t_sync_role (
    role_id         BIGINT PRIMARY KEY,
    account_id      BIGINT,
    role_name       VARCHAR(64),
    server_id       SMALLINT,
    gid             SMALLINT,
    create_time     DATETIME,
    last_login_time DATETIME,
    level           SMALLINT,
    _sync_time      DATETIME,
    KEY idx_account (account_id)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

-- ============ db_adm：配置与主数据（维表） ============

CREATE TABLE IF NOT EXISTS db_adm.t_sy_game (
    gid         SMALLINT PRIMARY KEY,
    game_name   VARCHAR(64),
    pgid        SMALLINT,
    launch_date DATE,
    status      VARCHAR(16)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

CREATE TABLE IF NOT EXISTS db_adm.t_sy_channel (
    cid          SMALLINT PRIMARY KEY,
    channel_name VARCHAR(32),
    channel_type VARCHAR(16)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

CREATE TABLE IF NOT EXISTS db_adm.t_sy_server (
    server_id   SMALLINT PRIMARY KEY,
    server_name VARCHAR(32),
    gid         SMALLINT,
    open_date   DATE,
    merge_group VARCHAR(8),
    merge_date  DATE
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

CREATE TABLE IF NOT EXISTS db_adm.t_sy_item (
    item_id   INT PRIMARY KEY,
    item_name VARCHAR(32),
    item_type VARCHAR(16)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

CREATE TABLE IF NOT EXISTS db_adm.t_sy_product (
    product_id   VARCHAR(16) PRIMARY KEY,
    product_name VARCHAR(64),
    price        DECIMAL(10,2),
    product_type VARCHAR(16)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

CREATE TABLE IF NOT EXISTS db_adm.t_sy_activity (
    activity_id   VARCHAR(8) PRIMARY KEY,
    activity_name VARCHAR(64),
    start_date    DATE,
    end_date      DATE,
    activity_type VARCHAR(16)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

CREATE TABLE IF NOT EXISTS db_adm.t_sy_version (
    app_version  VARCHAR(16) PRIMARY KEY,
    release_date DATE,
    release_note VARCHAR(128)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

CREATE TABLE IF NOT EXISTS db_adm.t_sy_account (
    account_id    BIGINT PRIMARY KEY,
    register_time DATETIME,
    pid           SMALLINT,
    cid           SMALLINT,
    status        VARCHAR(16)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

CREATE TABLE IF NOT EXISTS db_adm.t_sy_role (
    role_id         BIGINT PRIMARY KEY,
    account_id      BIGINT,
    role_name       VARCHAR(64),
    server_id       SMALLINT,
    cid             SMALLINT,
    pid             SMALLINT,
    gid             SMALLINT,
    create_time     DATETIME,
    last_login_time DATETIME,
    level           SMALLINT,
    KEY idx_cid (cid),
    KEY idx_create_day (create_time)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

-- ============ db_dw_dwd：明细事实层（支付；埋点明细在 ES） ============

CREATE TABLE IF NOT EXISTS db_dw_dwd.t_pay_di (
    store_day      INT NOT NULL,
    order_id       VARCHAR(32) PRIMARY KEY,
    pay_time       DATETIME,
    account_id     BIGINT,
    role_id        BIGINT,
    server_id      SMALLINT,
    pid            SMALLINT,
    gid            SMALLINT,
    cid            SMALLINT,
    channel_name   VARCHAR(32),
    product_id     VARCHAR(16),
    product_name   VARCHAR(64),
    product_type   VARCHAR(16),
    standard_price DECIMAL(10,2),
    amount         DECIMAL(10,2),
    status         VARCHAR(16),
    is_refund      TINYINT(1),
    is_first_pay   TINYINT(1),
    KEY idx_day (store_day),
    KEY idx_role (role_id)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

-- ============ db_dw_dws：轻度汇总层 ============

CREATE TABLE IF NOT EXISTS db_dw_dws.t_role_active_di (
    store_day        INT NOT NULL,
    role_id          BIGINT NOT NULL,
    account_id       BIGINT,
    server_id        SMALLINT,
    merge_group      VARCHAR(8),
    cid              SMALLINT,
    pid              SMALLINT,
    gid              SMALLINT,
    is_new_role      TINYINT(1),
    login_cnt        INT,
    online_minutes   INT,
    last_login_time  DATETIME,
    pay_cnt          INT,
    pay_amount       DECIMAL(10,2),
    is_first_pay_day TINYINT(1),
    ad_watch_cnt     INT,
    gacha_cnt        INT,
    PRIMARY KEY (store_day, role_id),
    KEY idx_role_day (role_id, store_day)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

CREATE TABLE IF NOT EXISTS db_dw_dws.t_server_channel_di (
    store_day      INT NOT NULL,
    server_id      SMALLINT,
    merge_group    VARCHAR(8),
    cid            SMALLINT,
    dau            INT,
    new_roles      INT,
    pay_amount     DECIMAL(12,2),
    pay_users      INT,
    online_minutes BIGINT,
    ad_watch_cnt   BIGINT,
    gacha_cnt      BIGINT,
    KEY idx_day (store_day)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

-- ============ db_dw_ads：应用层 ============

CREATE TABLE IF NOT EXISTS db_dw_ads.t_game_overview_di (
    store_day          INT PRIMARY KEY,
    dau                INT,
    new_users          INT,
    new_ratio          DOUBLE,
    ret_new_d1         DOUBLE,
    ret_new_d7         DOUBLE,
    dau_ret_d1         DOUBLE,
    pay_users          INT,
    pay_rate           DOUBLE,
    revenue            DECIMAL(12,2),
    refund_amount      DECIMAL(12,2),
    arpu               DOUBLE,
    arppu              DOUBLE,
    first_pay_users    INT,
    session_cnt        BIGINT,
    online_minutes     BIGINT,
    online_avg_min     DOUBLE,
    ad_watch_users     INT,
    ad_watch_cnt       BIGINT,
    gacha_users        INT,
    gacha_cnt          BIGINT
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

CREATE TABLE IF NOT EXISTS db_dw_ads.t_channel_ltv_wk (
    cohort_week  DATE,
    cid          SMALLINT,
    channel_name VARCHAR(32),
    reg_users    INT,
    ret_w1       DOUBLE,
    ret_w4       DOUBLE,
    ltv_7        DOUBLE,
    ltv_30       DOUBLE,
    pay_users_30 INT,
    pay_rate_30  DOUBLE,
    KEY idx_week (cohort_week)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

-- ============ db_meta：跑批日志 + 日历表（解决 int 日期跨月运算） ============

CREATE TABLE IF NOT EXISTS db_meta.t_etl_log (
    id         BIGINT AUTO_INCREMENT PRIMARY KEY,
    run_time   DATETIME,
    step       VARCHAR(64),
    store_day  INT,
    elapsed_ms INT,
    rows_affected BIGINT
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

-- 日历表：store_day 的次日/第7天/第30天（int 日期不能直接加减，跨月会错）
CREATE TABLE IF NOT EXISTS db_meta.t_day_seq (
    store_day INT PRIMARY KEY,
    next_day  INT,
    day7      INT,
    day30     INT
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;
