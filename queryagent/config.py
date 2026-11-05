"""运行配置：连接信息复用 gamedw/configs/infra.yaml。

LLM 优先级：环境变量 > infra.yaml > 默认值。
不配 LLM 时系统走纯规则路径，核心功能可用。
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gamedw.db import load_infra  # noqa: E402


def _load_llm_from_yaml() -> dict:
    """从 infra.yaml 读 llm 段；文件不存在或没配就返回空 dict。"""
    try:
        infra = load_infra()
        return infra.get("llm") or {}
    except Exception:
        return {}


_yaml_llm = _load_llm_from_yaml()


class Settings:
    # LLM：OpenAI 兼容接口（DeepSeek/Qwen/GLM 均可）
    # 优先级：环境变量 > infra.yaml > 默认值
    llm_api_key: str = os.environ.get("LLM_API_KEY") or _yaml_llm.get("api_key", "")
    llm_base_url: str = os.environ.get("LLM_BASE_URL") or _yaml_llm.get("base_url", "https://api.deepseek.com/v1")
    llm_model: str = os.environ.get("LLM_MODEL") or _yaml_llm.get("model", "deepseek-chat")

    # 执行护栏（业界标配）
    query_timeout_ms: int = 30_000
    row_cap: int = 100_000
    file_threshold: int = 5_000      # 超过该行数自动落 Excel/CSV
    max_limit: int = 10_000


SETTINGS = Settings()


def infra() -> dict:
    return load_infra()
