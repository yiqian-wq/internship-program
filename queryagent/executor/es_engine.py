"""ES 明细检索执行器（明细类问题走这里——多引擎路由的 ES 侧）。"""
from gamedw.db import es_client


def role_detail(role_id: int, events: list[str] = ("login",), limit: int = 100) -> dict:
    """按角色查埋点明细（跨月索引，按时间倒序）。"""
    es = es_client()
    out = {}
    for ev in events:
        res = es.search(
            index=f"mlog-{ev}-*",
            query={"term": {"role_id": role_id}},
            sort=[{"_id": {"order": "asc"}}] if ev != "login" else
                 [{"log_time": {"order": "desc", "missing": "_last"}}],
            size=min(limit, 1000),
        )
        hits = []
        for h in res["hits"]["hits"]:
            src = dict(h["_source"])
            src["_index"] = h["_index"]
            hits.append(src)
        out[ev] = hits
    return out


def aggregate_by_dim(event: str, dim_field: str, store_day: int | None = None,
                     size: int = 50) -> list[dict]:
    """ES terms 聚合（示例：某天各渠道广告观看次数）——多引擎路由的 ES 聚合能力。"""
    es = es_client()
    body: dict = {"size": 0, "aggs": {"by_dim": {"terms": {"field": dim_field, "size": size}}}}
    if store_day:
        body["query"] = {"term": {"store_day": store_day}}
    res = es.search(index=f"mlog-{event}-*", body=body)
    return [{"dim": b["key"], "count": b["doc_count"]}
            for b in res["aggregations"]["by_dim"]["buckets"]]
