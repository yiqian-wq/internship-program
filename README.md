# 🎮 GameDW + QueryAgent

> 游戏数仓 + 运营自助取数 Agent 全栈系统

## 一句话介绍

一个面向游戏运营的**自助取数 Agent**：用户用自然语言提问（"上个月各渠道的 DAU 和流水"、"查角色 50007436 的登录记录"），Agent 自动理解意图、生成 DSL、编译 SQL、走双引擎（MySQL 聚合 + ES 明细）返回结构化结果。

## 架构概览

```
用户自然语言
     │
     ▼
┌─────────────┐
│  LangGraph  │  6 节点状态图 + 自纠回路
│    Agent    │  classify → recall → parse → validate → execute → present
└──────┬──────┘
       │ DSL / Template
       ▼
┌───────────────┐
│  QueryAgent   │  DSL 编译器 + 语义层 + 校验器
└───┬───────┬───┘
    │       │
    ▼       ▼
┌──────┐ ┌──────┐
│ MySQL│ │  ES  │  双引擎
│ 聚合 │ │ 明细 │
└──────┘ └──────┘
    │       │
    ▼       ▼
  ODS → DWD → DWS → ADS  数仓分层
```

### 双引擎设计

| 场景 | 引擎 | 说明 |
|---|---|---|
| 聚合查询（DAU/流水/留存） | **MySQL** | 走 DWS 层宽表，确定性 SQL 编译 |
| 明细检索（角色行为日志） | **Elasticsearch** | 按月滚动索引，role_id + event 过滤 |

### LangGraph Agent 节点

| 节点 | 职责 |
|---|---|
| `classify` | 意图分类（data_query / semantic_qa / unknown） |
| `recall` | 从示例库召回 fewshots，注入 prompt |
| `parse` | 规则优先 → 模板路径 → LLM 兜底，生成 DSL |
| `validate` | DSL 静态校验 + filter 值归一化 |
| `execute` | MySQL 编译 SQL / ES DSL 检索 |
| `present` | 结果解读（留存自动算极值日期、LTV 自动算回本周期） |

自纠回路：`parse → validate` 最多重试 2 轮，校验不过自动回到 parse 重生成。

### 意图分流

```
classify
  ├─ unknown / semantic_qa → 友好拒绝（一句话提示）
  └─ data_query → rule_parse
                    ├─ kind=template → 次留/LTV 模板 SQL
                    ├─ kind=dsl      → 规则命中的 measures+dimensions
                    ├─ kind=detail   → ES 明细检索（需 8 位 role_id）
                    └─ kind=None     → llm_parse 兜底
```

## 快速开始

### 1. 环境准备

```bash
# 1) 虚拟环境
python -m venv .venv
.venv\Scripts\activate       # Windows
source .venv/bin/activate    # macOS/Linux

pip install -r requirements.txt

# 2) MySQL 8.0 + Elasticsearch 8.15
#    MySQL: deploy/docker-compose.yml 一键拉起
#    ES:   直接下载安装，bin/elasticsearch.bat（Windows 需管理员终端）

# 3) 配置凭据
cp gamedw/configs/infra.yaml.example gamedw/configs/infra.yaml
# 填入 MySQL 密码、ES URL、LLM API Key
```

### 2. 造数 + ETL + 质检

```bash
# 造数（首次/重装用 --fresh，--scale 控制规模）
python -m gamedw.sim.generate --fresh --scale 0.2

# ETL 全流程 ODS → DWD → DWS → ADS + ES 索引
python -m gamedw.etl.runner

# 双引擎质检（MySQL vs ES 一致性）
python -m gamedw.scripts.validate
# 目标：12/12 PASS
```

### 3. 冒烟测试 Agent

```bash
# 规则命中（measures + dimensions）
python -m queryagent.cli ask "上个月各渠道的DAU和流水"

# 模板命中（次留 / LTV）
python -m queryagent.cli ask "9月的新增次日留存"

# LLM 兜底（超出内置规则）
python -m queryagent.cli ask "抖音9月的ARPU是多少"

# ES 明细检索（需 8 位 role_id）
python -m queryagent.cli ask "查角色50007436的登录记录"

# API 服务
python -m api.main            # http://localhost:8000/chat
```

### 4. 多轮追问

```python
from datetime import date
from queryagent.agent.graph import Agent
from queryagent.knowledge.registry import Registry

agent = Agent(Registry(date(2026, 10, 16)))

s1 = agent.ask("近7天各渠道的DAU")
s2 = agent.ask("那抖音的呢", session_dsl=s1.dsl)   # 继承时间 + 加过滤
s3 = agent.ask("换上个月看看", session_dsl=s2.dsl)  # 覆盖时间范围
```

## 目录结构

```
internship-program/
├── gamedw/                     # 数仓层
│   ├── sim/                    # 模拟数据生成器
│   ├── etl/                    # ETL 跑批（ODS→DWD→DWS→ADS）
│   ├── scripts/                # 质检脚本
│   ├── configs/
│   │   ├── warehouse.yaml      # 数仓配置（时间跨度/渠道/平台）
│   │   └── infra.yaml.example  # 基础设施模板
│   └── deploy/
│       └── docker-compose.yml  # MySQL + ES 一键拉起
│
├── queryagent/                 # Agent 层
│   ├── agent/
│   │   ├── graph.py            # LangGraph 状态图
│   │   └── nodes.py            # 6 个业务节点 + classify
│   ├── dsl/
│   │   ├── validator.py        # DSL 静态校验
│   │   └── time_parser.py      # 时间表达式解析（含公测首周等业务锚点）
│   ├── compiler/
│   │   └── compose.py         # DSL → SQL 确定性编译
│   ├── executor/
│   │   ├── mysql_engine.py     # MySQL 执行器
│   │   └── es_engine.py        # ES 执行器
│   ├── knowledge/
│   │   ├── registry.py         # 语义层注册表
│   │   └── semantic_models.yaml  # 指标/维度/模板/同义词定义
│   ├── llm.py                  # LLM 客户端（DeepSeek / OpenAI 兼容）
│   ├── cli.py                  # CLI 入口
│   └── api/main.py             # FastAPI 入口
│
└── api/                        # API 层
```

## 设计原则

1. **DSL-first**：LLM 只生成 DSL JSON，SQL 由 compose.py 确定性编译，可审计可复现
2. **规则优先**：measures + dimensions + templates 覆盖 80% 问题，0 token 成本
3. **静默替换 = 不可接受**：时间范围/过滤条件解析失败直接报错，不用默认值
4. **双引擎解耦**：聚合走 MySQL（确定性），明细走 ES（高性能），互不干扰

## 技术栈

| 组件 | 技术 |
|---|---|
| 数仓 | MySQL 8.0（分区表 + CTE + 字典表 JOIN） |
| 明细存储 | Elasticsearch 8.15（按月滚动索引模板） |
| Agent | LangGraph（状态图 + 自纠回路） |
| LLM | DeepSeek-chat / OpenAI 兼容接口 |
| API | FastAPI |
| Python | 3.12 |
