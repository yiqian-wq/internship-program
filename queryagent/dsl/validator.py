"""静态校验器：DSL 合法性 + 语义模型约束。错误结构化输出（供 Agent 自纠循环回喂）。"""
from dataclasses import dataclass

from queryagent.dsl.schema import DataQueryDsl, FilterNode
from queryagent.knowledge.registry import Registry


@dataclass
class ValidationError:
    code: str
    field: str
    message: str
    hint: str = ""

    def dict(self):
        return {"code": self.code, "field": self.field, "message": self.message, "hint": self.hint}


def _walk_filters(node: FilterNode | None):
    if node is None:
        return
    if node.type.is_logical:
        for f in node.fields or []:
            yield from _walk_filters(f)
    else:
        yield node


def normalize_filter_values(dsl: DataQueryDsl, registry: Registry) -> None:
    """LLM 路径补全：把 DSL.filter 里的字符串别名翻译成值域 int ID（原地修改 DSL）。

    LLM 会把 filter.value 写成"抖音"/"TapTap"/"安卓"这种别名，
    但 compose.py 和 validator 期望的是 registry 里定义的 int ID（4001/2001/2）。
    规则路径（rule_parse）在组装 DSL 时已经做过翻译，LLM 路径没做——这里补上。
    """
    ds = registry.dataset_def(dsl.dataset)
    for leaf in _walk_filters(dsl.filter):
        dim_def = registry.dimension_def(dsl.dataset, leaf.dimension)
        if dim_def is None:
            continue
        dim_name = leaf.dimension

        # 单值: EQ/NE/GT/GTE/LT/LTE
        if isinstance(leaf.value, str):
            vid = registry.lookup_value(leaf.value, dim_name)
            if vid is not None:
                leaf.value = vid
            elif leaf.value.strip().isdigit():     # LLM 可能写成字符串数字 "4001"
                leaf.value = int(leaf.value.strip())
        # 列表值: IN/NIN
        elif isinstance(leaf.value, list):
            translated = []
            for v in leaf.value:
                if isinstance(v, str):
                    vid = registry.lookup_value(v, dim_name)
                    if vid is not None:
                        translated.append(vid)
                    elif v.strip().isdigit():
                        translated.append(int(v.strip()))
                    else:
                        translated.append(v)      # 翻不动的保留原值(validator 会报错)
                else:
                    translated.append(v)
            leaf.value = translated


def validate(dsl: DataQueryDsl, registry: Registry) -> list[ValidationError]:
    errors: list[ValidationError] = []
    if dsl.dataset not in registry.datasets:
        return [ValidationError("UNKNOWN_DATASET", "dataset",
                                f"数据集 {dsl.dataset} 不存在",
                                f"可用: {', '.join(registry.datasets)}")]
    ds = registry.dataset_def(dsl.dataset)
    dim_names = {x["name"] for x in ds.get("dimensions", [])}

    for dim in dsl.dimensions:
        if dim not in dim_names:
            errors.append(ValidationError("UNKNOWN_DIMENSION", f"dimensions:{dim}",
                                          f"数据集 {dsl.dataset} 没有维度「{dim}」",
                                          f"可用维度: {', '.join(sorted(dim_names))}"))
    for agg in dsl.aggregations:
        if registry.measure_def(dsl.dataset, agg.measure) is None:
            errors.append(ValidationError("UNKNOWN_MEASURE", f"aggregations:{agg.measure}",
                                          f"数据集 {dsl.dataset} 没有指标「{agg.measure}」",
                                          f"可用指标: " + ", ".join(
                                              m["name"] for m in ds.get("measures", []))))
    if dsl.time_range:
        s, e = dsl.time_range
        if s > e:
            errors.append(ValidationError("BAD_TIME_RANGE", "time_range",
                                          f"开始 {s} 晚于结束 {e}"))
        if len(str(s)) != 8 or len(str(e)) != 8:
            errors.append(ValidationError("BAD_TIME_FORMAT", "time_range",
                                          "时间必须是 int YYYYMMDD"))

    def _check_leaf(node: FilterNode):
        if node.dimension and node.dimension not in dim_names:
            errors.append(ValidationError("UNKNOWN_FILTER_DIM", f"filter:{node.dimension}",
                                          f"过滤维度「{node.dimension}」不存在",
                                          f"可用维度: {', '.join(sorted(dim_names))}"))
        dim = registry.dimension_def(dsl.dataset, node.dimension) if node.dimension else None
        if dim and dim.get("type") == "int" and isinstance(node.value, list):
            if not all(isinstance(v, int) for v in node.value):
                errors.append(ValidationError("TYPE_MISMATCH", f"filter:{node.dimension}",
                                              f"维度 {node.dimension} 是整型，过滤值必须全为整数",
                                              "用维度值域别名（如「抖音」）或整数 id"))

    for leaf in _walk_filters(dsl.filter):
        _check_leaf(leaf)

    if not dsl.aggregations and not dsl.dimensions:
        errors.append(ValidationError("EMPTY_QUERY", "dimensions/aggregations",
                                      "至少需要维度或指标"))
    return errors
