"""结果导出：>阈值自动落文件（对齐真实 task_record 的 FILE_PATH 概念）。"""
from datetime import datetime
from pathlib import Path

import pandas as pd

EXPORT_DIR = Path(__file__).parents[2] / "queryagent" / "exports"


def export(df: pd.DataFrame, stem: str = "result") -> Path:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = EXPORT_DIR / f"{stem}_{ts}.csv"
    df.to_csv(path, index=False, encoding="utf-8-sig")   # 带 BOM，Excel 直接打开不乱码
    return path
