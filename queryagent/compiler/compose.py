"""SQL 编译器：DSL → MySQL 方言。移植真实公司 ComposeUtils 的 fieldCompose/filterCompose 思想，
并叠加语义模型（维度表达式/关联/值域）与 int 日期处理。
"""
from queryagent.dsl.schema import AggregationType, DataQueryDsl, FilterNode, FilterType
from queryagent.knowledge.registry import Registry

AGG_SQL = {
    AggregationType.DISTINCTCOUNT: "COUNT(DISTINCT {expr})",
    AggregationType.LONGSUM: "SUM({expr})",
    AggregationType.DOUBLESUM: "SUM({expr})",
    AggregationType.LONGMAX: "MAX({expr})",
    AggregationType.LONGMIN: "MIN({expr})",
    AggregationType.AVG: "AVG({expr})",
    AggregationType.COUNT: "COUNT(CASE WHEN {cond} THEN 1 END)",
}


def _leaf_sql(node: FilterNode, registry: Registry, dataset: str,
              table_alias: str = "") -> str:
    dim = registry.dimension_def(dataset, node.dimension)
    col = dim["expr"] if dim else node.dimension
    expr = f"{table_alias}.{col}" if table_alias else col
    op = node.type.sql_op
    if node.type in (FilterType.IN, FilterType.NIN):
        vals = ", ".join(str(int(v)) if dim and dim.get("type") == "int" else f"'{v}'"
                         for v in (node.value or []))
        return f"{expr} {op} ({vals})"
    v = node.value
    if dim and dim.get("type") == "int":
        cond = f"{expr} {op} {int(v)}"
    else:
        cond = f"{expr} {op} '{v}'"
    return cond


def filter_to_sql(node: FilterNode | None, registry: Registry, dataset: str,
                  table_alias: str = "") -> str | None:
    if node is None:
        return None
    if node.type.is_logical:
        parts = [filter_to_sql(f, registry, dataset, table_alias)
                 for f in node.fields or []]
        parts = [p for p in parts if p]
        if not parts:
            return None
        joiner = " AND " if node.type == FilterType.AND else " OR "
        return "(" + joiner.join(parts) + ")"
    return _leaf_sql(node, registry, dataset, table_alias)


def compile_dsl(dsl: DataQueryDsl, registry: Registry) -> str:
    ds = registry.dataset_def(dsl.dataset)
    table = ds["table"]
    T = "t"   # 主表别名，避免 JOIN 后同名列歧义

    selected: list[str] = []
    group_by: list[str] = []
    joins: list[str] = []
    join_aliases: set[str] = set()

    for dim_name in dsl.dimensions:
        dim = registry.dimension_def(dsl.dataset, dim_name)
        col = dim["expr"] if dim else dim_name
        qualified = f"{T}.{col}"
        selected.append(qualified)
        group_by.append(qualified)
        rel = dim.get("related") if dim else None
        if rel:
            alias = rel["table"].split(".")[-1]
            if alias not in join_aliases:
                join_aliases.add(alias)
                joins.append(f"LEFT JOIN {rel['table']} AS {alias} ON {alias}.{rel['join_on']} = {qualified}")
            selected.append(f"{alias}.{rel['column']}")
            group_by.append(f"{alias}.{rel['column']}")

    where_parts: list[str] = []
    if dsl.time_range:
        where_parts.append(f"{T}.store_day BETWEEN {dsl.time_range[0]} AND {dsl.time_range[1]}")
    fsql = filter_to_sql(dsl.filter, registry, dsl.dataset, table_alias=T)
    if fsql:
        where_parts.append(fsql)

    select_list = list(selected)
    for agg in dsl.aggregations:
        m = registry.measure_def(dsl.dataset, agg.measure)
        col = m["expr"]
        expr = f"{T}.{col}"
        cond = m.get("filter")          # 指标自带过滤（如 is_new_role / pay_cnt > 0）
        if cond:
            # 指标 filter 里的列名也加上主表别名（简单处理：空格/比较符之前的列名都加 t.）
            import re as _re
            cond = _re.sub(r"\b([a-zA-Z_]\w*)\b",
                           lambda mm: f"{T}.{mm.group(1)}" if mm.group(1).lower()
                                      not in ("case", "when", "then", "else", "end",
                                              "and", "or", "not", "in", "is", "null",
                                              "true", "false")
                                      else mm.group(1),
                           cond)
            body = {
                AggregationType.DISTINCTCOUNT: f"COUNT(DISTINCT CASE WHEN {cond} THEN {expr} END)",
                AggregationType.COUNT: f"COUNT(CASE WHEN {cond} THEN 1 END)",
                AggregationType.LONGSUM: f"SUM(CASE WHEN {cond} THEN {expr} ELSE 0 END)",
                AggregationType.DOUBLESUM: f"SUM(CASE WHEN {cond} THEN {expr} ELSE 0 END)",
                AggregationType.LONGMAX: f"MAX(CASE WHEN {cond} THEN {expr} END)",
                AggregationType.LONGMIN: f"MIN(CASE WHEN {cond} THEN {expr} END)",
                AggregationType.AVG: f"AVG(CASE WHEN {cond} THEN {expr} END)",
            }[AggregationType(agg.type)]
        else:
            body = AGG_SQL[AggregationType(agg.type)].format(expr=expr)
        select_list.append(f"{body} AS {agg.alias}")

    sql = f"SELECT {', '.join(select_list)}\nFROM {table} AS {T}"
    for j in joins:
        sql += f"\n{j}"
    if where_parts:
        sql += "\nWHERE " + "\n  AND ".join(where_parts)
    if group_by:
        sql += "\nGROUP BY " + ", ".join(group_by)
    if dsl.order:
        aliases = {agg.alias for agg in dsl.aggregations}
        order_parts = []
        for f in dsl.order.fields:
            dim = registry.dimension_def(dsl.dataset, f)
            if f in aliases:
                order_parts.append(f)                       # 指标别名原样
            elif dim:
                order_parts.append(f"{T}.{dim['expr']}")    # 维度 → 主表别名.物理列
            else:
                order_parts.append(f)
        direction = "DESC" if dsl.order.desc else "ASC"
        sql += f"\nORDER BY {', '.join(order_parts)} {direction}"
    sql += f"\nLIMIT {dsl.limit}"
    return sql
