"""核心单元测试（不依赖数据库：registry 用固定锚点日期）。"""
from datetime import date

import pytest

from queryagent.dsl import time_parser
from queryagent.dsl.schema import (Aggregation, AggregationType, DataQueryDsl,
                                   FilterNode, FilterType)
from queryagent.dsl.validator import validate
from queryagent.knowledge.registry import Registry

ANCHOR = date(2026, 10, 16)      # 数仓数据最后一天
REG = Registry(ANCHOR)


# ---------- 时间解析器 ----------
@pytest.mark.parametrize("text,expected", [
    ("近7天的DAU", (20261010, 20261016)),
    ("昨天的新增", (20261015, 20261015)),
    ("上周各渠道DAU", (20261005, 20261011)),
    ("上个月流水", (20260901, 20260930)),
    ("9月的流水", (20260901, 20260930)),
    ("2026-09-01到2026-09-15的流水", (20260901, 20260915)),
])
def test_time_parser(text, expected):
    assert time_parser.parse_time(text, ANCHOR) == expected


def test_time_parser_unknown_returns_none():
    assert time_parser.parse_time("明天天气怎么样", ANCHOR) is None


# ---------- 召回（同义词） ----------
def test_find_measures():
    hits = REG.find_measures("抖音上个月每天的DAU和流水")
    assert {"dau", "revenue"} <= set(hits)


def test_find_value_alias():
    hits = REG.find_value("抖音的用户", dim_name="渠道")
    assert ("role_active_daily", "渠道", 4001) in hits


# ---------- 校验器 ----------
def test_validator_rejects_unknown_dimension():
    dsl = DataQueryDsl(dataset="role_active_daily",
                       dimensions=["不存在的维度"],
                       aggregations=[Aggregation(type=AggregationType.DISTINCTCOUNT, measure="dau")])
    errs = validate(dsl, REG)
    assert any(e.code == "UNKNOWN_DIMENSION" for e in errs)


def test_validator_rejects_bad_time_range():
    dsl = DataQueryDsl(dataset="role_active_daily",
                       aggregations=[Aggregation(type=AggregationType.DISTINCTCOUNT, measure="dau")],
                       time_range=[20261016, 20261010])
    errs = validate(dsl, REG)
    assert any(e.code == "BAD_TIME_RANGE" for e in errs)


# ---------- 编译器（golden SQL） ----------
def test_compile_douyin_daily_dau_revenue():
    dsl = DataQueryDsl(
        dataset="role_active_daily",
        dimensions=["日期", "渠道"],
        aggregations=[Aggregation(type=AggregationType.DISTINCTCOUNT, measure="dau"),
                      Aggregation(type=AggregationType.LONGSUM, measure="revenue")],
        filter=FilterNode(type=FilterType.EQ, dimension="渠道", value=4001),
        time_range=[20260901, 20260930],
        order=None, limit=1000,
    )
    sql = __import__("queryagent.compiler.compose", fromlist=["compile_dsl"]).compile_dsl(dsl, REG)
    assert "COUNT(DISTINCT role_id) AS dau" in sql
    assert "SUM(pay_amount) AS revenue" in sql
    assert "cid = 4001" in sql
    assert "BETWEEN 20260901 AND 20260930" in sql
    assert "GROUP BY store_day, cid" in sql
    assert "LEFT JOIN db_adm.t_sy_channel" in sql
