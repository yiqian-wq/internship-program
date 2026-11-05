"""命令行入口（演示/教学用）：

    python -m queryagent.cli ask "抖音上个月每天的DAU和流水"
    python -m queryagent.cli ask "上周新增用户的次日留存"
    python -m queryagent.cli ask "查角色50000260的登录记录"
    python -m queryagent.cli dsl dsl.json        # 手写 DSL 文件直接执行（阶段3作业验收用）
"""
import argparse
import json
import sys

from gamedw.db import _utf8_console, mysql_connect

from queryagent.agent.graph import Agent
from queryagent.config import SETTINGS
from queryagent.dsl.schema import DataQueryDsl
from queryagent.knowledge.registry import Registry

_agent: Agent | None = None


def get_agent() -> Agent:
    global _agent
    if _agent is None:
        con = mysql_connect()
        with con.cursor() as c:
            c.execute("SELECT MAX(store_day) FROM db_dw_dws.t_role_active_di")
            row = c.fetchone()
        anchor_date = row[0]
        from datetime import date
        anchor = date(int(str(anchor_date)[:4]), int(str(anchor_date)[4:6]), int(str(anchor_date)[6:]))
        _agent = Agent(Registry(anchor))
    return _agent


def _print_state(state: dict):
    print("=" * 72)
    if state.get("error"):
        print(f"[错误] {state['error']}")
        return
    if state.get("dsl") is not None:
        print(f"[DSL]（来源: {state.get('source')}）")
        print(json.dumps(json.loads(state["dsl"].model_dump_json()), ensure_ascii=False, indent=2))
    if state.get("template_id"):
        print(f"[模板] {state['template_id']}  时间范围: {state.get('time_range')}")
    if state.get("detail"):
        print(f"[明细检索] {state['detail'].model_dump_json(ensure_ascii=False)}")
    if state.get("sql"):
        print(f"[SQL]\n{state['sql']}")
    rows = state.get("rows")
    if rows is not None and not rows.empty:
        print(f"[结果] {state.get('total')} 行，耗时 {state.get('elapsed')}ms")
        print(rows.head(10).to_string(index=False))
    print(f"[图表建议] {state.get('chart')}")
    print(f"[解读] {state['summary']}")


def cmd_ask(args):
    agent = get_agent()
    state = agent.ask(args.question)
    _print_state(state)
    if args.json:
        out = {k: v for k, v in state.items()
               if k in ("summary", "chart", "dsl", "sql", "total", "elapsed") and v is not None}
        if "dsl" in out:
            out["dsl"] = json.loads(out["dsl"].model_dump_json())
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))


def cmd_dsl(args):
    agent = get_agent()
    dsl = DataQueryDsl.model_validate(json.loads(open(args.file, encoding="utf-8").read()))
    state = agent.graph.invoke({"question": "(DSL 文件执行)", "dsl": dsl, "retries": 0})
    _print_state(state)


def main():
    _utf8_console()
    ap = argparse.ArgumentParser(prog="queryagent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_ask = sub.add_parser("ask", help="自然语言取数")
    p_ask.add_argument("question")
    p_ask.add_argument("--json", action="store_true", help="额外输出 JSON 结果")
    p_dsl = sub.add_parser("dsl", help="执行 DSL JSON 文件")
    p_dsl.add_argument("file")
    args = ap.parse_args()
    if args.cmd == "ask":
        cmd_ask(args)
    elif args.cmd == "dsl":
        cmd_dsl(args)


if __name__ == "__main__":
    main()
    _ = SETTINGS  # 保持 import 以便配置生效提示
