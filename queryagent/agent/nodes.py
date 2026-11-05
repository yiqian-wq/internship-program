"""Agent 节点实现：意图分类、召回、规则解析（混合解析的"规则先行"）、LLM 兜底、执行、呈现。

设计对应《详细设计》第 3 节 S0-S7。规则路径完全离线可跑（不依赖 LLM API Key）。
"""
from __future__ import annotations

import json
import re
from datetime import datetime

import pandas as pd

from queryagent.compiler.compose import compile_dsl
from queryagent.config import SETTINGS
from queryagent.dsl import time_parser
from queryagent.dsl.schema import (Aggregation, DataQueryDsl, DetailQuery,
                                   FilterNode, FilterType, OrderBy)
from queryagent.dsl.validator import validate
from queryagent.executor import es_engine, exporter
from queryagent.executor.mysql_engine import execute as mysql_execute
from queryagent.knowledge import templates
from queryagent.knowledge.registry import Registry

EVENT_WORDS = {"登录": "login", "登出": "logout", "抽卡": "property",
               "道具": "property", "广告": "ad", "激励视频": "ad"}


class FriendlyError(Exception):
    """带解决提示的用户可见错误。"""


# ---------------------------------------------------------------- S0 意图分类
def classify(question: str, registry: Registry,
             session_dsl=None) -> str:
    # 多轮追问：有会话记忆 + 追问句式 → 直接当 data_query
    if session_dsl is not None and re.match(r"^(那|还有|再看|换)", question):
        return "data_query"
    # 定义类关键词优先
    if re.search(r"什么是|是什么意思|怎么定义|口径|解释一下|含义", question):
        return "semantic_qa"
    # 取数相关关键词
    if (registry.find_measures(question) or registry.find_template(question)
            or re.search(r"\d{8,}", question)
            or registry.find_dimensions(question)
            or re.search(r"明细|记录|日志|查一下|查询|登录|登出|消费", question)):
        return "data_query"
    return "unknown"


# ---------------------------------------------------------------- S2 规则解析
def rule_parse(question: str, registry: Registry,
               session_dsl: DataQueryDsl | None = None) -> dict:
    """规则先行：同义词/值域/时间解析器能吃掉的请求直接出结果（0 token，毫秒级）。

    返回 {"kind": "dsl"|"template"|"detail", ...}；规则吃不掉返回 {"kind": None}。
    """
    # 多轮改写：追问句式 + 会话里有上一轮 DSL → 在上一轮基础上改
    if session_dsl is not None and re.match(r"^(那|还有|再看|换)", question):
        base = session_dsl.model_dump()
        base.setdefault("filter", None)
        values = registry.find_value(question)
        if values:
            ds, dim, vid = values[0]
            base["filter"] = {"type": "$EQ", "dimension": dim, "value": vid}
        t = time_parser.parse_time(question, registry.anchor, registry.launch_date)
        if t:
            base["time_range"] = list(t)
        base["source"] = "rule"
        return {"kind": "dsl", "dsl": DataQueryDsl.model_validate(base), "note": "多轮改写"}

    # 明细检索（走 ES）
    if re.search(r"明细|记录|日志|查一下|查询", question):
        m = re.search(r"(\d{8,})", question)
        if not m:
            raise FriendlyError(
                "查明细需要给角色 ID（至少 8 位数字）。"
                "例如「查角色50000260的登录记录」。")
        role_id = int(m.group(1))
        events = [v for k, v in EVENT_WORDS.items() if k in question] or ["login"]
        return {"kind": "detail", "detail": DetailQuery(role_id=role_id, events=events)}

    # 复杂指标模板（口径固定、SQL 固定、只填参数）
    tid = registry.find_template(question)
    if tid:
        t = time_parser.parse_time(question, registry.anchor, registry.launch_date)
        if t is None:
            # 用户命中了模板但时间表达解析不了 → 报错提示
            raise FriendlyError(
                f"识别到模板「{tid}」但没看懂时间范围。"
                f"试试说「9月的{tid}」、「公测首周」、「近7天的{tid}」。")
        return {"kind": "template", "template_id": tid, "time_range": list(t)}

    # 普通聚合 DSL
    measures = registry.find_measures(question)
    if not measures:
        return {"kind": None}

    dataset = registry.measure_dataset(question, measures[0])

    dims = registry.find_dimensions(question)
    group_dims: list[str] = []
    for dim in dims:
        synonyms = [dim, *(registry.dimension_def(dataset, dim) or {}).get("synonyms", [])]
        if any(re.search(rf"(各|每个|按|分|不同){re.escape(s)}", question) for s in synonyms if s):
            group_dims.append(dim)
        elif dim == "日期" and re.search(r"每天|按天|每日|趋势", question):
            group_dims.append(dim)

    # 维度值 → 过滤条件（如"抖音"→ 渠道=4001；"安卓和iOS"→ 平台 IN (1,2)）
    value_map: dict[str, list[int]] = {}
    for ds, dim, vid in registry.find_value(question):
        if ds == dataset:
            value_map.setdefault(dim, []).append(vid)
    conds: list[FilterNode] = []
    for dim, ids in value_map.items():
        if len(ids) == 1:
            conds.append(FilterNode(type=FilterType.EQ, dimension=dim, value=ids[0]))
        else:
            conds.append(FilterNode(type=FilterType.IN, dimension=dim, value=ids))

    t = time_parser.parse_time(question, registry.anchor, registry.launch_date)
    if t is None:
        raise FriendlyError(
            "没看懂时间范围。试试说「近7天的DAU」、「上个月的DAU」、「公测首周DAU」。")

    order = None
    if "日期" in group_dims:
        order = OrderBy(fields=["日期"], desc=False)
    elif group_dims and measures:
        order = OrderBy(fields=[measures[0]], desc=True)

    dsl = DataQueryDsl(
        dataset=dataset,
        dimensions=group_dims,
        aggregations=[Aggregation(type=_agg_type(registry, dataset, m), measure=m)
                      for m in measures],
        filter=conds[0] if len(conds) == 1 else (
            FilterNode(type=FilterType.AND, fields=conds) if conds else None),
        time_range=list(t),
        order=order,
        source="rule",
    )
    return {"kind": "dsl", "dsl": dsl}


def _agg_type(registry: Registry, dataset: str, measure: str) -> str:
    m = registry.measure_def(dataset, measure)
    return (m or {}).get("agg", "LONGSUM")


# ---------------------------------------------------------------- S2b LLM 兜底解析
def llm_parse(question: str, registry: Registry,
              recalled: dict | None = None, errors: list[dict] | None = None) -> DataQueryDsl:
    from queryagent import llm
    from queryagent.agent.prompts import build_system_prompt, build_user_prompt

    brief = recalled or registry.dataset_brief("role_active_daily")
    system = build_system_prompt()
    user = build_user_prompt(brief, question, errors)
    raw = llm.chat_json(system, user)
    data = json.loads(raw)
    data["source"] = "llm"
    return DataQueryDsl.model_validate(data)


# ---------------------------------------------------------------- S5 执行
def execute_state(state: dict, registry: Registry) -> None:
    if state.get("template_id"):
        tdef = registry.template_def(state["template_id"])
        sql = templates.build_template(tdef["template_id"], registry, state.get("time_range"))
        df, elapsed, truncated = mysql_execute(sql)
        state["sql"] = sql
        state["rows"], state["total"], state["elapsed"] = df, len(df), elapsed
        state["truncated"] = truncated
        return
    if state.get("detail"):
        dq: DetailQuery = state["detail"]
        data = es_engine.role_detail(dq.role_id, dq.events, dq.limit)
        rows = []
        for ev, hits in data.items():
            for h in hits:
                rows.append({"event": ev, **{k: v for k, v in h.items() if k != "_index"}})
        df = pd.DataFrame(rows)
        state["rows"], state["total"] = df, len(df)
        state["summary_extra"] = f"命中 {len(df)} 条明细（ES 检索）"
        return

    dsl: DataQueryDsl = state["dsl"]
    sql = compile_dsl(dsl, registry)
    df, elapsed, truncated = mysql_execute(sql)
    state["sql"] = sql
    state["rows"], state["total"], state["elapsed"] = df, len(df), elapsed
    state["truncated"] = truncated
    if truncated or len(df) > SETTINGS.file_threshold:
        path = exporter.export(df)
        state["file"] = str(path)


# ---------------------------------------------------------------- S7 呈现（确定性模板，不依赖 LLM）
def present(state: dict, registry: Registry) -> None:
    if state.get("error"):
        return
    df: pd.DataFrame | None = state.get("rows")
    if df is None or df.empty:
        state["chart"] = "无数据"
        state["summary"] = (state.get("summary_extra") or
                            "查询成功但没有数据。建议放宽时间范围或筛选条件。")
        return

    # 模板专属呈现：次留自动算均值极值、LTV 自动找第一名
    # state["template_id"] 存的是 semantic_models.yaml 里的中文 name
    # （find_template 返回 t["name"]，不是 t["template_id"]）
    if state.get("template_id") == "新增次日留存" and "ret_new_d1" in df.columns:
        avg = pd.to_numeric(df["ret_new_d1"]).mean()
        state["chart"] = f"折线图：store_day × ret_new_d1（共 {len(df)} 天）"
        state["summary"] = (f"区间内平均新增次日留存 {avg * 100:.1f}%；"
                            f"最高 {df['ret_new_d1'].max() * 100:.1f}%（{df['store_day'].iloc[df['ret_new_d1'].idxmax()]}），"
                            f"最低 {df['ret_new_d1'].min() * 100:.1f}%（{df['store_day'].iloc[df['ret_new_d1'].idxmin()]}）。")
        return
    if state.get("template_id") == "渠道LTV" and "ltv_30" in df.columns:
        state["chart"] = "柱状图：channel_name × ltv_30"
        top = df.iloc[0]
        state["summary"] = (f"30 日 LTV 最高的渠道是 {top['channel_name']}（{top['ltv_30']} 元），"
                            f"共 {len(df)} 个渠道进入对比。")
        return
    if state.get("detail"):
        state["chart"] = "明细表格"
        state["summary"] = state.get("summary_extra", f"返回 {len(df)} 条明细")
        return

    date_col = next((c for c in ("store_day", "日期") if c in df.columns), None)
    numeric = [c for c in df.columns if c not in ("store_day", "日期")
               and pd.api.types.is_numeric_dtype(df[c])]
    if date_col:
        state["chart"] = f"折线图：{date_col} × {', '.join(numeric[:2]) or '指标'}"
    elif len(df) <= 30 and numeric:
        dim_cols = [c for c in df.columns if c not in numeric]
        state["chart"] = f"柱状图：{(dim_cols[0] if dim_cols else '维度')} × {numeric[0]}"
    else:
        state["chart"] = "数据表格"

    head = ", ".join(f"{c}={df[c].iloc[0]}" for c in df.columns[:3])
    tail = ("结果已超过行数上限，完整结果见导出文件。" if state.get("truncated")
            else (f"超过 {SETTINGS.file_threshold} 行已导出文件。" if state.get("file") else ""))
    state["summary"] = (f"查询返回 {len(df):,} 行（耗时 {state.get('elapsed', 0)}ms）。"
                        f"首行：{head}。{tail}")
