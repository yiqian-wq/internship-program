"""评测跑分脚本（课程阶段 6 的种子版）：

    python -m queryagent.evals.run            # 结构校验（规则路径，离线）
    python -m queryagent.evals.run --exec     # 额外执行 SQL 冒烟
"""
import argparse
import json
import sys
from datetime import date
from pathlib import Path

import yaml

from gamedw.db import _utf8_console, mysql_connect

from queryagent.agent import nodes
from queryagent.compiler.compose import compile_dsl
from queryagent.dsl.validator import validate
from queryagent.executor.mysql_engine import execute
from queryagent.knowledge.registry import Registry


def main():
    _utf8_console()
    ap = argparse.ArgumentParser()
    ap.add_argument("--exec", action="store_true", help="执行 SQL 冒烟")
    args = ap.parse_args()

    con = mysql_connect()
    with con.cursor() as c:
        c.execute("SELECT MAX(store_day) FROM db_dw_dws.t_role_active_di")
        sd = c.fetchone()[0]
    anchor = date(int(str(sd)[:4]), int(str(sd)[4:6]), int(str(sd)[6:]))
    registry = Registry(anchor)

    cases = yaml.safe_load((Path(__file__).parent / "cases.yaml").read_text(encoding="utf-8"))["cases"]
    passed = 0
    results = []
    for i, case in enumerate(cases, 1):
        q = case["question"]
        exp = case["expect"]
        rule = nodes.rule_parse(q, registry)
        ok, why = True, []

        if exp.get("template"):
            if rule.get("template_id") != exp["template"]:
                ok, why = False, [f"模板应为 {exp['template']}，实际 {rule.get('template_id')}"]
        elif rule.get("kind") == "dsl":
            dsl = rule["dsl"]
            got_dims = set(dsl.dimensions)
            want_dims = set(exp.get("dims", []))
            if not want_dims <= got_dims:
                ok = False
                why.append(f"缺维度 {want_dims - got_dims}")
            got_measures = {a.measure for a in dsl.aggregations}
            want_measures = set(exp.get("measures", []))
            if not want_measures <= got_measures:
                ok = False
                why.append(f"缺指标 {want_measures - got_measures}")
            filter_dims = set()
            node = dsl.filter
            stack = [node] if node else []
            while stack:
                n = stack.pop()
                if n and n.type.is_logical:
                    stack.extend(n.fields or [])
                elif n:
                    filter_dims.add(n.dimension)
            want_fd = set(exp.get("filter_dims", []))
            if not want_fd <= filter_dims:
                ok = False
                why.append(f"缺过滤维度 {want_fd - filter_dims}")
            errs = validate(dsl, registry)
            if errs:
                ok = False
                why.append(f"校验失败: {[e.message for e in errs]}")
            if ok and args.exec:
                try:
                    sql = compile_dsl(dsl, registry)
                    _, _, _ = execute(sql)
                except Exception as e:  # noqa: BLE001
                    ok, why = False, [f"执行失败: {e}"]
        else:
            ok, why = False, [f"规则未命中（kind={rule.get('kind')}）"]

        results.append((i, q, ok, "; ".join(why)))
        passed += int(ok)

    print("=" * 72)
    for i, q, ok, why in results:
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] #{i:>2} {q}  {why if why else ''}")
    print("=" * 72)
    print(f"通过 {passed}/{len(cases)}（{passed / len(cases) * 100:.0f}%）"
          + ("  [含 SQL 执行冒烟]" if args.exec else ""))
    sys.exit(0 if passed == len(cases) else 1)


if __name__ == "__main__":
    main()
