"""LangGraph 状态图（详细设计第 5 节）：
classify → recall → parse(rule→llm兜底) → validate(失败≤2轮回喂) → execute → present
"""
from __future__ import annotations

import json
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from queryagent.agent import nodes
from queryagent.dsl.schema import DataQueryDsl, DetailQuery
from queryagent.dsl.validator import validate
from queryagent.knowledge.registry import Registry


class AgentState(TypedDict, total=False):
    question: str
    intent: str
    session_dsl: DataQueryDsl | None      # 会话记忆：上一轮 DSL（多轮追问的基础）
    dsl: DataQueryDsl | None
    detail: DetailQuery | None
    template_id: str | None
    time_range: list[int] | None
    recalled: dict | None
    parse_errors: list[str]
    validation_errors: list[dict]
    retries: int
    sql: str | None
    rows: Any
    total: int
    elapsed: int
    truncated: bool
    file: str | None
    chart: str | None
    summary: str
    error: str | None
    source: str


class Agent:
    """一次初始化，反复提问。session 记忆由调用方保存上一轮 DSL。"""

    def __init__(self, registry: Registry):
        self.registry = registry
        self.graph = self._build()

    # ---------- 节点 ----------
    def n_classify(self, s: AgentState) -> AgentState:
        s["intent"] = nodes.classify(s["question"], self.registry,
                                     s.get("session_dsl"))
        return s

    def n_recall(self, s: AgentState) -> AgentState:
        q = s["question"]
        reg = self.registry
        measures = reg.find_measures(q)
        dims = reg.find_dimensions(q)
        s["recalled"] = {
            "measures": measures,
            "dimensions": dims,
            "template": reg.find_template(q),
            "brief": reg.dataset_brief(reg.measure_dataset(q, measures[0]) if measures
                                       else next(iter(reg.datasets)), dims or None,
                                       measures or None),
        }
        return s

    def n_parse(self, s: AgentState) -> AgentState:
        q = s["question"]
        session_dsl: DataQueryDsl | None = s.get("session_dsl")
        rule = nodes.rule_parse(q, self.registry, session_dsl)
        if rule["kind"] == "dsl":
            s["dsl"], s["source"] = rule["dsl"], rule["dsl"].source
            return s
        if rule["kind"] == "template":
            s["template_id"] = rule["template_id"]
            s["time_range"] = rule["time_range"]
            return s
        if rule["kind"] == "detail":
            s["detail"] = rule["detail"]
            return s
        # 规则未命中 → LLM 兜底
        from queryagent import llm
        if not llm.available():
            raise nodes.FriendlyError(
                "这个问题超出了内置规则能理解的范围，且未配置 LLM API Key（LLM_API_KEY 环境变量）。\n"
                "配置后即可处理任意自然语言取数问题。")
        dsl = nodes.llm_parse(q, self.registry, s.get("recalled"), s.get("validation_errors"))
        # LLM 会把 filter.value 写成字符串别名（"抖音"/"安卓"），翻译成 registry 里的 int ID
        from queryagent.dsl.validator import normalize_filter_values
        normalize_filter_values(dsl, self.registry)
        s["dsl"], s["source"] = dsl, "llm"
        return s

    def n_validate(self, s: AgentState) -> AgentState:
        if s.get("template_id") or s.get("detail") or s.get("dsl") is None:
            return s
        errors = validate(s["dsl"], self.registry)
        s["validation_errors"] = [e.dict() for e in errors]
        s["retries"] = s.get("retries", 0)
        if errors:
            s["retries"] += 1
        return s

    def n_execute(self, s: AgentState) -> AgentState:
        nodes.execute_state(s, self.registry)
        return s

    def n_present(self, s: AgentState) -> AgentState:
        if s.get("error"):
            return s
        nodes.present(s, self.registry)
        return s

    def n_friendly_error(self, s: AgentState) -> AgentState:
        err = s.get("error") or "未能理解这个问题。"
        if s.get("validation_errors"):
            err += "\n校验问题：" + json.dumps(s["validation_errors"], ensure_ascii=False)
        s["summary"] = err
        return s

    def n_reject_question(self, s: AgentState) -> AgentState:
        """不相关问题 / 定义问题 → 统一友好一句话。"""
        intent = s.get("intent", "unknown")
        if intent == "semantic_qa":
            s["summary"] = "这是取数 Agent，不回答定义类问题。如需查询指标请直接描述需求，例如「上个月各渠道的DAU」。"
        else:
            s["summary"] = "抱歉，我只能回答游戏数据取数相关的问题。试试说「近7天的DAU」、「9月的新增次日留存」。"
        return s

    # ---------- 路由 ----------
    def r_after_classify(self, s: AgentState) -> str:
        intent = s.get("intent", "data_query")
        if intent in ("semantic_qa", "unknown"):
            return "reject_question"
        return "recall"

    def r_after_parse(self, s: AgentState) -> str:
        if s.get("error"):
            return "friendly_error"
        if not (s.get("dsl") or s.get("template_id") or s.get("detail")):
            s["error"] = ("未能理解这个问题。规则与示例库均未命中，"
                          "可换个说法或配置 LLM_API_KEY 启用完整解析。")
            return "friendly_error"
        return "validate"

    def r_after_validate(self, s: AgentState) -> str:
        if s.get("template_id") or s.get("detail"):
            return "execute"
        if s.get("validation_errors"):
            if s.get("retries", 0) <= 2:
                return "parse"          # 自纠循环：错误回喂重新解析
            s["error"] = "多次生成均未通过校验。"
            return "friendly_error"
        return "execute"

    def _wrap_node(self, fn):
        def wrapped(s: AgentState) -> AgentState:
            try:
                return fn(s)
            except nodes.FriendlyError as e:
                return {**s, "error": str(e)}
            except Exception as e:      # noqa: BLE001 —— 统一转成用户可见错误
                return {**s, "error": f"执行失败：{e}"}
        return wrapped

    # ---------- 组图 ----------
    def _build(self):
        g = StateGraph(AgentState)
        g.add_node("classify", self._wrap_node(self.n_classify))
        g.add_node("recall", self._wrap_node(self.n_recall))
        g.add_node("parse", self._wrap_node(self.n_parse))
        g.add_node("validate", self._wrap_node(self.n_validate))
        g.add_node("execute", self._wrap_node(self.n_execute))
        g.add_node("present", self._wrap_node(self.n_present))
        g.add_node("friendly_error", self._wrap_node(self.n_friendly_error))
        g.add_node("reject_question", self._wrap_node(self.n_reject_question))

        g.set_entry_point("classify")
        g.add_conditional_edges("classify", self.r_after_classify,
                                {"recall": "recall", "reject_question": "reject_question"})
        g.add_edge("reject_question", END)
        g.add_edge("recall", "parse")
        g.add_conditional_edges("parse", self.r_after_parse,
                                {"validate": "validate", "friendly_error": "friendly_error"})
        g.add_conditional_edges("validate", self.r_after_validate,
                                {"execute": "execute", "parse": "parse",
                                 "friendly_error": "friendly_error"})
        g.add_edge("execute", "present")
        g.add_edge("present", END)
        g.add_edge("friendly_error", END)
        return g.compile()

    # ---------- 对外入口 ----------
    def ask(self, question: str, session_dsl: DataQueryDsl | None = None) -> AgentState:
        state: AgentState = self.graph.invoke(
            {"question": question, "session_dsl": session_dsl, "retries": 0})
        # 会话记忆：成功取数后记住最后一轮 DSL（多轮追问的基础）
        if isinstance(state.get("dsl"), DataQueryDsl):
            state["session_dsl"] = state["dsl"]
        return state
