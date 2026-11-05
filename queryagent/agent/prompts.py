"""Prompt 构建（详细设计 S2 LLM 兜底）：语义模型摘要 + few-shot + 结构化输出要求。"""
import json


def build_system_prompt() -> str:
    return (
        "你是游戏数据取数助手，负责把自然语言翻译成取数 DSL（JSON）。\n"
        "规则：\n"
        "1. 只能使用给定的数据集/维度/指标，禁止编造字段；\n"
        "2. 时间范围由系统解析，你在 time_range 里填 [start_int, end_int]（int YYYYMMDD），"
        "不确定就填 null；\n"
        "3. 输出 JSON 字段：dataset, dimensions(数组，用维度中文名), aggregations(数组：{type, measure}), "
        "filter(可空：{type:'$EQ'/'$IN'/'$AND'..., dimension, value}，逻辑节点用 fields 数组), "
        "order(可空：{fields, desc}), limit(默认1000)；\n"
        "4. 聚合 type 可选 DISTINCTCOUNT/LONGSUM/LONGMAX/LONGMIN/AVG/COUNT；\n"
        "5. 只输出 JSON，不要解释。"
    )


def build_user_prompt(dataset_brief: dict, question: str,
                      errors: list[dict] | None = None) -> str:
    parts = [f"【可用语义模型】\n{json.dumps(dataset_brief, ensure_ascii=False)}"]
    shots = _fewshots()
    if shots:
        parts.append("【示例】\n" + json.dumps(shots, ensure_ascii=False))
    if errors:
        parts.append("【上一轮输出校验失败，必须修复】\n" + json.dumps(errors, ensure_ascii=False))
    parts.append(f"【用户问题】{question}\n【输出 JSON】")
    return "\n\n".join(parts)


def _fewshots() -> list[dict]:
    from pathlib import Path
    f = Path(__file__).parents[1] / "knowledge" / "fewshots.yaml"
    if f.exists():
        import yaml
        return yaml.safe_load(f.read_text(encoding="utf-8")).get("examples", [])
    return []
