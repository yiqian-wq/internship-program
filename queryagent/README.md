# queryagent —— 运营自助取数 Agent（教学参考实现）

> **定位**：《实战课程手册》的**教师版参考实现**——学生按手册阶段 3-7 自己重建，卡住时对照本实现
> 的对应模块；组织者用它演示完整效果。**不建议直接发给学生当答案**（防作弊约定见手册末节）。
> 设计依据：《详细设计-Agent自助取数系统.md》（业界方案对齐：语义层约束 + 混合解析 + 示例记忆）。

## 当前能力（P1 全量 + P2 核心）

| 能力 | 说明 | 无 LLM 可用？ |
|---|---|---|
| 语义模型 | `knowledge/semantic_models.yaml`：数据集/指标/维度/同义词/值域/关联 | ✅ |
| 混合解析 | 规则先行（同义词命中 0 token 毫秒级）→ LLM 兜底（JSON 强约束） | 规则部分 ✅ |
| 时间解析 | "上周/近7天/上个月/9月"→ store_day 区间（确定性代码，锚点=数仓最新日期） | ✅ |
| 静态校验 | 结构/枚举/类型/时间区间，错误结构化输出 | ✅ |
| SQL 编译 | DSL→MySQL 方言（含 int 日期、维度关联、指标自带过滤） | ✅ |
| 多引擎路由 | 聚合走 MySQL；明细检索（"查角色 xxx 的登录记录"）走 ES | ✅ |
| 复杂指标模板 | 次留（自 join + 日历表）、渠道 LTV（ADS 表）——DSL 表达不了的口径走模板 | ✅ |
| 多轮会话 | "那 B 站呢？"自动沿用上一轮框架换筛选（FastAPI 会话记忆） | ✅ |
| 评测 | 20 题种子集 + 跑分脚本（阶段 6 学生扩到 120 题 + 消融） | ✅ |
| LangGraph | 状态图编排（classify→recall→parse→validate→execute→present + 自纠回路） | 图本身 ✅ |

## 快速开始

```bash
# 前置：gamedw 数仓已起数据（见 gamedw/docs/环境搭建指南.md）

# 不配 LLM：规则路径演示（推荐先跑这个）
.venv/bin/python -m queryagent.cli ask "抖音上个月每天的DAU和流水"
.venv/bin/python -m queryagent.cli ask "上周新增用户的次日留存"
.venv/bin/python -m queryagent.cli ask "各渠道LTV怎么样"
.venv/bin/python -m queryagent.cli ask "查角色50000260的登录记录"

# 配置 LLM 后（处理任意自然语言问法）
export LLM_API_KEY=sk-xxx                      # Windows: set LLM_API_KEY=sk-xxx
export LLM_BASE_URL=https://api.deepseek.com/v1
export LLM_MODEL=deepseek-chat

# API 服务（多轮会话）
.venv/bin/python -m uvicorn queryagent.api.main:app --port 8100
curl -X POST http://127.0.0.1:8100/chat -H "Content-Type: application/json" \
  -d '{"session_id":"u1","message":"抖音上个月每天的DAU和流水"}'

# 评测与测试
.venv/bin/python -m queryagent.evals.run --exec
.venv/bin/python -m pytest queryagent/tests -q
```

## 架构（对应《详细设计》编号）

```
question → [classify] → [recall 语义召回] → [parse 规则→LLM兜底]
              → [validate 静态校验] ←失败≤2回喂← ┘
              → [execute 路由: DSL→MySQL / 模板 / ES明细] → [present 图表+解读]
知识层: semantic_models.yaml + fewshots.yaml + templates.py
治理:   只读账号 + 强制LIMIT(≤1万) + 超时30s + 行数上限10万 + 超阈值落CSV(utf-8-sig)
```

## 与课程阶段的对照（学生重建指引）

| 课程阶段 | 参考实现模块 |
|---|---|
| 阶段 3 DSL+编译器 | `dsl/schema.py`、`dsl/validator.py`、`compiler/compose.py`、`knowledge/registry.py` |
| 阶段 4 LLM 入门 | `llm.py`、`agent/prompts.py`（结构化输出 + few-shot 注入） |
| 阶段 5 Agent 编排 | `agent/nodes.py`（规则解析器是精华）、`agent/graph.py`、`api/main.py` |
| 阶段 6 评测 | `evals/cases.yaml`（20 题种子）、`evals/run.py` |
| 阶段 8 亮点 | `executor/es_engine.py`（ES 聚合/明细）、`knowledge/templates.py`（模板双轨） |

## 已知边界（诚实清单）

- 澄清反问尚未实现（歧义问句当前按默认口径回答）——课程阶段 5 的练习点；
- 行级权限/审计表未落地（详细设计第 7 节，阶段 8 扩展）；
- 前端聊天页未包含（阶段 7，React/ECharts 自建）；
- 规则解析覆盖高频问法；长尾问法需 LLM_API_KEY。
