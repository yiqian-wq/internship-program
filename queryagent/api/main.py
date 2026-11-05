"""FastAPI 服务：

    uvicorn queryagent.api.main:app --reload --port 8100
    POST /chat  {"session_id": "u1", "message": "抖音上个月每天的DAU和流水"}
"""
import json

from fastapi import FastAPI
from pydantic import BaseModel

from gamedw.db import _utf8_console, mysql_connect

from queryagent.agent.graph import Agent
from queryagent.knowledge.registry import Registry

app = FastAPI(title="运营自助取数 Agent", version="0.1.0")
_agent: Agent | None = None
_sessions: dict[str, dict] = {}          # 简易会话记忆：session_id → 上一轮 DSL


def get_agent() -> Agent:
    global _agent
    if _agent is None:
        con = mysql_connect()
        with con.cursor() as c:
            c.execute("SELECT MAX(store_day) FROM db_dw_dws.t_role_active_di")
            sd = c.fetchone()[0]
        from datetime import date
        anchor = date(int(str(sd)[:4]), int(str(sd)[4:6]), int(str(sd)[6:]))
        _agent = Agent(Registry(anchor))
    return _agent


class ChatRequest(BaseModel):
    session_id: str = "default"
    message: str


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/chat")
def chat(req: ChatRequest):
    agent = get_agent()
    state = agent.ask(req.message, _sessions.get(req.session_id))
    if isinstance(state.get("session_dsl"), object):
        _sessions[req.session_id] = state.get("session_dsl")
    rows = state.get("rows")
    out = {
        "summary": state.get("summary"),
        "chart": state.get("chart"),
        "error": state.get("error"),
        "source": state.get("source"),
        "dsl": json.loads(state["dsl"].model_dump_json()) if state.get("dsl") is not None else None,
        "sql": state.get("sql"),
        "total": state.get("total"),
        "elapsed_ms": state.get("elapsed"),
        "file": state.get("file"),
    }
    if rows is not None and not rows.empty:
        out["columns"] = list(rows.columns)
        out["rows"] = rows.head(50).to_dict(orient="records")
    return json.loads(json.dumps(out, ensure_ascii=False, default=str))


if __name__ == "__main__":
    _utf8_console()
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8100)
