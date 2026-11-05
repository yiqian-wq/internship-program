# gamedw —— 模拟小公司游戏数仓（MySQL + Elasticsearch）

为「游戏运营自助取数 Agent」项目提供数据底座：一套**贴近小公司真实形态**的游戏数仓，
包含模拟的业务库/埋点源系统、双引擎存储、分层 ETL、以及埋好的业务剧本数据。

- 模拟对象：某小型游戏发行公司的放置卡牌手游《苍穹物语》，2025-09-01 公测，T+1 数仓
- 数据周期：2025-09-01 ~ 2026-10-16（约 13.5 个月）
- 量级：小爆款水平 —— 注册 38 万、DAU 1~2 万、日流水万元级；全库约 6800 万行
- **存储架构（小公司标配）**：
  - **Elasticsearch**：埋点明细（登录/登出/道具/广告），按月索引 `mlog-{事件}-{YYYYMM}`（按天过滤靠 store_day 字段），可明细检索
  - **MySQL**：业务库同步（订单/账号快照）+ 数仓分层（`db_adm` 维表 → `db_dw_dwd` → `db_dw_dws` → `db_dw_ads`）
  - 埋点明细只存 ES；数仓的"角色日汇总"由 ETL 用 **ES 聚合**加工后写入 MySQL（避免明细双写）
- **跨平台**：Windows / macOS / Linux 全兼容，环境搭建见 [docs/环境搭建指南.md](docs/环境搭建指南.md)

## 快速开始

```bash
# 0) 环境：先看 docs/环境搭建指南.md（Windows 用户直接看这一步）
#    推荐：cd gamedw/deploy && docker compose up -d
#    然后改 configs/infra.yaml 里的 MySQL 密码（compose 版为 gamedw）

# 1) 创建虚拟环境（项目根目录）
python -m venv .venv && .venv/bin/pip install -r requirements.txt   # Windows: .venv\Scripts\activate

# 2) 生成源数据（写入 ES + MySQL；正式数据去掉 --scale，约 10~20 分钟）
python -m gamedw.sim.generate --fresh --scale 0.2

# 3) 数仓跑批（ES 聚合 + MySQL 分层 SQL，模拟凌晨 crontab）
python -m gamedw.etl.runner

# 4) 双引擎质检（10 项 PASS 即环境完好）
python -m gamedw.scripts.validate

# 日常开发：只重跑某天（增量 + 报表全量重算）
python -m gamedw.etl.runner --dt 2026-09-25
```

## 架构：小公司形态（MySQL + ES，命名对齐公司真实规范）

```
「业务库 MySQL」+「客户端埋点」            （gamedw/sim 模拟生成）
        │ 业务库同步(crontab)        │ 埋点上报
        ▼                            ▼
MySQL db_mlog                    ES mlog-{login|logout|property|ad}-{YYYYMMDD}
  t_sdk_pay_order 订单同步          埋点明细，按天索引，可检索（保留脏数据：
  t_sync_account/role 用户快照       缺失渠道 cid=-1、测试账号、明细节点可查）
        │                            │
        │      gamedw/etl：dim/订单全量重刷(SQL) + ES 聚合加工(15_es_role_active)
        ▼                            ▼
MySQL db_adm      维表：t_sy_game/channel/server/item/product/activity/version + 清洗后 t_sy_account/role
MySQL db_dw_dwd   t_pay_di（订单去重 + 首充/退款标记；埋点明细层=ES）
MySQL db_dw_dws   t_role_active_di（角色×日核心宽表，含 ES 聚合的登录/时长/广告/抽卡）
                  t_server_channel_di
        ▼
MySQL db_dw_ads   t_game_overview_di（运营核心日报）、t_channel_ltv_wk（渠道周cohort）
MySQL db_meta     t_etl_log 跑批日志、t_day_seq 日历表（int 日期跨月运算）
```

分区列统一为 `store_day`（int YYYYMMDD，对齐真实数仓），跨月运算查 `db_meta.t_day_seq` 日历表；
维度三维 `pid`（平台）/`gid`（游戏）/`cid`（渠道）、设备号 `dev` 对齐真实埋点表。

与真实小公司的对应关系与简化（诚实清单）：
| 真实世界 | 本模拟 |
|---|---|
| MySQL 业务库 + 神策/自研埋点上报 | `gamedw/sim` 逐日生成写入 ES / MySQL |
| Logstash/脚本同步 ES→数仓 | `etl/es_agg.py`：ES 聚合加工成 DWS |
| crontab / DolphinScheduler | `etl/runner.py`（支持 --dt 单日回刷） |
| MySQL 慢查询痛点 | 真实保留：ADS 留存 join 需索引（idx_role_day），全量重跑约 1~3 分钟 |
| 按天分区表 | 索引/按 store_day 有序写入，语义等价 |

## 数据里埋好的业务剧本（分析/演示素材）

| 剧本 | 窗口 | 分析价值 |
|---|---|---|
| 公测增长曲线 | 2025-09-01 起 | 新游冷启动、自然量衰减 |
| 国庆/双旦/五一活动 | 各节日窗口 | 活动效果评估 |
| **春节版本「新春祭」** | 2026-02-14 ~ 03-02 | DAU/付费全年峰值 |
| 买量投放 ×3 | 2025-11 / 2026-03 / 2026-07 | 渠道 ROI：抖音量最大、质量最差 |
| **v2.1 新手引导事故** | 2026-09-24 ~ 10-01，10-02 修复 | 新增次留下滑 ~8pt 后恢复（近期热点） |
| 合服 | 2026-06-12（s06/07/08 → M1） | 区服合并语义 |
| 渠道质量分化 | 全周期 | TapTap/B站 LTV 高、抖音买量 LTV 低 |
| 脏数据四件套 | 见《数据字典》 | 清洗逻辑/语义层规则素材 |

## 目录结构

```
gamedw/
├── configs/warehouse.yaml    # 模拟参数：留存曲线、渠道、版本、活动、剧本窗口、脏数据
├── configs/infra.yaml        # MySQL/ES 连接配置（可用环境变量覆盖，Windows 同样适用）
├── sim/generate.py           # 源系统模拟器（cohort 活跃模型；埋点→ES bulk，订单/快照→MySQL）
├── etl/
│   ├── runner.py             # 跑批入口（SQL 步骤 + ES 聚合步骤混排；--dt 单日 / --step 分层）
│   ├── es_agg.py             # ES 聚合 → t_role_active_di（埋点进数仓的关键链路）
│   └── sql/                  # MySQL 方言分层 SQL（${dt} 参数化，幂等）
├── scripts/validate.py       # 双引擎质检
├── deploy/docker-compose.yml # 课程环境一键拉起（MySQL 8 + ES 8）
├── docs/数据字典.md          # 全部表结构/索引映射 + 口径约定 + 已知瑕疵
├── docs/环境搭建指南.md      # Windows / macOS 双路径环境搭建
└── data/                     # （DuckDB 旧版遗留，可删除；现以 MySQL+ES 为准）
```

## 二次开发提示

- 调整量级/剧本：改 `configs/warehouse.yaml` 后 `--fresh` 重新生成（种子固定，结果可复现）。
- 追加新指标：优先在 DWS/ADS 层加 SQL 任务；埋点新事件先改 ES 模板（`db.py` ES_MAPPINGS）再加聚合。
- "语义在数仓、Agent 只做编排"的边界不变——这是后续指标语义层的动机。
