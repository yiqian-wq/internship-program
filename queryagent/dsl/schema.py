"""取数 DSL：字段对齐真实公司 data-center 的 DataQueryParm（Java），pydantic 强约束。"""
from enum import Enum
from typing import Literal, Optional, Union

from pydantic import BaseModel, Field, model_validator


class AggregationType(str, Enum):
    DISTINCTCOUNT = "DISTINCTCOUNT"
    LONGSUM = "LONGSUM"
    DOUBLESUM = "DOUBLESUM"
    LONGMAX = "LONGMAX"
    LONGMIN = "LONGMIN"
    AVG = "AVG"
    COUNT = "COUNT"


class FilterType(str, Enum):
    AND = "$AND"
    OR = "$OR"
    EQ = "$EQ"
    NE = "$NE"
    IN = "$IN"
    NIN = "$NIN"
    GT = "$GT"
    GTE = "$GTE"
    LT = "$LT"
    LTE = "$LTE"

    @property
    def is_logical(self) -> bool:
        return self in (FilterType.AND, FilterType.OR)

    @property
    def sql_op(self) -> str:
        return {
            "$EQ": "=", "$NE": "<>", "$GT": ">", "$GTE": ">=",
            "$LT": "<", "$LTE": "<=",
            "$IN": "IN", "$NIN": "NOT IN",
        }[self.value]


class Aggregation(BaseModel):
    type: AggregationType
    measure: str                    # 语义指标名（如 dau / revenue），编译时查语义模型
    name: Optional[str] = None      # 输出列别名，默认用指标名

    @property
    def alias(self) -> str:
        return self.name or self.measure


class FilterNode(BaseModel):
    type: FilterType
    dimension: Optional[str] = None                  # 叶子：语义维度名
    value: Optional[Union[int, float, str, list]] = None
    fields: Optional[list["FilterNode"]] = None      # 逻辑节点：子条件

    @model_validator(mode="after")
    def _check(self):
        if self.type.is_logical:
            if not self.fields:
                raise ValueError(f"{self.type.value} 节点必须包含 fields")
        else:
            if self.dimension is None or self.value is None:
                raise ValueError(f"叶子条件 {self.type.value} 需要 dimension 和 value")
        return self


class OrderBy(BaseModel):
    fields: list[str]
    desc: bool = True


class DataQueryDsl(BaseModel):
    """一次聚合取数请求。时间范围单独放 time_range（由时间解析器产出，LLM 不碰日期算术）。"""
    dataset: str
    dimensions: list[str] = []
    aggregations: list[Aggregation] = []
    filter: Optional[FilterNode] = None
    time_range: Optional[list[int]] = Field(default=None, min_length=2, max_length=2)
    order: Optional[OrderBy] = None
    limit: int = Field(default=1000, ge=1, le=10000)
    source: Literal["rule", "llm", "manual"] = "manual"

    @model_validator(mode="after")
    def _check(self):
        if not self.aggregations and not self.dimensions:
            raise ValueError("至少需要 dimensions 或 aggregations 之一")
        return self


class DetailQuery(BaseModel):
    """明细检索请求（走 ES）：按角色查事件明细。"""
    role_id: int
    events: list[Literal["login", "logout", "property", "ad"]] = ["login"]
    limit: int = Field(default=100, ge=1, le=1000)
