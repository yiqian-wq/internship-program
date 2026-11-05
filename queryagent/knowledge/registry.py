"""知识层：加载语义模型，提供 召回/同义词匹配/值域解析（规则解析的弹药库）。

召回策略（P1 版）：同义词子串/关键词打分，确定性、零依赖。
进阶（P3+）：可替换为 embedding 向量检索——接口不变。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import yaml

KNOWLEDGE_DIR = Path(__file__).parent
WAREHOUSE_YAML = KNOWLEDGE_DIR.parent.parent / "gamedw" / "configs" / "warehouse.yaml"


class DimensionDef(dict):
    pass


class DatasetDef(dict):
    pass


def _d(s) -> date:
    return s if isinstance(s, date) else date.fromisoformat(str(s))


class Registry:
    def __init__(self, anchor: date):
        raw = yaml.safe_load((KNOWLEDGE_DIR / "semantic_models.yaml").read_text(encoding="utf-8"))
        self.raw = raw
        self.anchor = anchor                     # 数据锚点日 = 数仓最新日期（T+1）
        # 业务锚点：从 warehouse.yaml 读公测日期
        self.launch_date: date | None = None
        try:
            wh = yaml.safe_load(WAREHOUSE_YAML.read_text(encoding="utf-8"))
            self.launch_date = _d(wh["meta"]["data_start"])
        except Exception:
            pass
        self.datasets: dict[str, DatasetDef] = {d["name"]: d for d in raw["datasets"]}
        self.templates = {t["name"]: t for t in raw.get("metric_templates", [])}
        # 同义词索引：词 → (dataset, kind, name)
        self._syn_index: dict[str, list[tuple[str, str, str]]] = {}
        for d in raw["datasets"]:
            for m in d.get("measures", []):
                for w in [m["name"], *m.get("synonyms", [])]:
                    self._syn_index.setdefault(w.lower(), []).append((d["name"], "measure", m["name"]))
            for dim in d.get("dimensions", []):
                for w in [dim["name"], *dim.get("synonyms", [])]:
                    self._syn_index.setdefault(w.lower(), []).append((d["name"], "dimension", dim["name"]))
            for t in raw.get("metric_templates", []):
                for w in [t["name"], *t.get("synonyms", [])]:
                    self._syn_index.setdefault(w.lower(), []).append(("", "template", t["name"]))
        # 维度值域：alias → (dataset, dim_name, value_id)
        self._value_index: dict[str, tuple[str, str, int]] = {}
        for d in raw["datasets"]:
            for dim in d.get("dimensions", []):
                for v in dim.get("values", []):
                    for w in [v["label"], *v.get("aliases", [])]:
                        self._value_index[w.lower()] = (d["name"], dim["name"], v["id"])

    # ---------- 召回 ----------
    def find_measures(self, text: str, dataset: str | None = None) -> list[str]:
        t = text.lower()
        hits = []
        for w, refs in sorted(self._syn_index.items(), key=lambda kv: -len(kv[0])):
            if w in t:
                for ds, kind, name in refs:
                    if kind == "measure" and (dataset is None or ds == dataset) and name not in hits:
                        hits.append(name)
        return hits

    def find_dimensions(self, text: str) -> list[str]:
        t = text.lower()
        hits = []
        for w, refs in sorted(self._syn_index.items(), key=lambda kv: -len(kv[0])):
            if w in t:
                for ds, kind, name in refs:
                    if kind == "dimension" and name not in hits:
                        hits.append(name)
        return hits

    def find_template(self, text: str) -> str | None:
        t = text.lower()
        for w, refs in sorted(self._syn_index.items(), key=lambda kv: -len(kv[0])):
            if w in t:
                for _, kind, name in refs:
                    if kind == "template":
                        return name
        return None

    def measure_dataset(self, text: str, measure: str) -> str:
        """某指标在问题中被命中时，返回它所属的数据集（默认第一个数据集）。"""
        t = text.lower()
        for w, refs in sorted(self._syn_index.items(), key=lambda kv: -len(kv[0])):
            if w in t:
                for ds, kind, name in refs:
                    if kind == "measure" and name == measure:
                        return ds
        return next(iter(self.datasets))

    def find_value(self, text: str, dim_name: str | None = None) -> list[tuple[str, str, int]]:
        """返回 [(dataset, dim_name, value_id)]，按别名长度降序（'抖音买量'优先于'抖音'）。"""
        t = text.lower()
        hits = []
        for w, ref in sorted(self._value_index.items(), key=lambda kv: -len(kv[0])):
            if w in t and (dim_name is None or ref[1] == dim_name):
                if ref not in hits:
                    hits.append(ref)
        return hits

    def lookup_value(self, alias: str, dim_name: str | None = None) -> int | None:
        """精确别名 → int value_id（供 LLM 路径 filter 值翻译使用）。
        例如 lookup_value('抖音', '渠道') → 4001。找不到返回 None。"""
        key = alias.strip().lower()
        ref = self._value_index.get(key)
        if ref is None:
            return None
        if dim_name and ref[1] != dim_name:
            return None
        return ref[2]

    def dataset_def(self, name: str) -> DatasetDef:
        return self.datasets[name]

    def dimension_def(self, dataset: str, dim: str) -> dict | None:
        for d in self.datasets[dataset].get("dimensions", []):
            if d["name"] == dim:
                return d
        return None

    def measure_def(self, dataset: str, measure: str) -> dict | None:
        for m in self.datasets[dataset].get("measures", []):
            if m["name"] == measure:
                return m
        return None

    def template_def(self, name: str) -> dict:
        return self.templates[name]

    # ---------- 供 LLM prompt 使用的语义摘要（只放召回的片段，不放全量） ----------
    def dataset_brief(self, name: str, dim_names: list[str] | None = None,
                      measure_names: list[str] | None = None) -> dict:
        d = self.dataset_def(name)
        dims = [x for x in d.get("dimensions", []) if dim_names is None or x["name"] in dim_names]
        ms = [x for x in d.get("measures", []) if measure_names is None or x["name"] in measure_names]
        return {
            "dataset": name,
            "table": d["table"],
            "description": d.get("description", ""),
            "dimensions": [{"name": x["name"], "type": x["type"],
                            "synonyms": x.get("synonyms", [])} for x in dims],
            "measures": [{"name": x["name"], "agg": x["agg"], "expr": x["expr"],
                          "description": x.get("description", "")} for x in ms],
        }
