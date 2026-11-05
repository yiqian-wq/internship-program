"""LLM 封装：OpenAI 兼容接口。未配置 API Key 时 available()=False，系统走纯规则路径。"""
from queryagent.config import SETTINGS

_client = None


def available() -> bool:
    return bool(SETTINGS.llm_api_key)


def get_client():
    global _client
    if _client is None:
        from openai import OpenAI
        _client = OpenAI(api_key=SETTINGS.llm_api_key, base_url=SETTINGS.llm_base_url)
    return _client


def chat_json(system: str, user: str) -> str:
    """要求模型输出纯 JSON。"""
    res = get_client().chat.completions.create(
        model=SETTINGS.llm_model,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        response_format={"type": "json_object"},
        temperature=0.1,
    )
    return res.choices[0].message.content or "{}"


def chat_text(system: str, user: str) -> str:
    res = get_client().chat.completions.create(
        model=SETTINGS.llm_model,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        temperature=0.3,
    )
    return res.choices[0].message.content or ""
