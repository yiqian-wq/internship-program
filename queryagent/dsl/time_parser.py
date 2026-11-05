"""时间解析器（确定性代码）：把"上周/近7天/9月"解析成 store_day 区间。

业界共识：LLM 算日期必然幻觉，时间解析必须由规则代码完成。
锚点日期 = 数仓数据的最新日期（T+1 语义：数据截至昨天），而不是真实系统时间。

新增：业务锚点支持（公测首周、上线月、版本X后等），从 warehouse.yaml 读取。
"""
import re
from calendar import monthrange
from datetime import date, timedelta


def _sd(d: date) -> int:
    return int(d.strftime("%Y%m%d"))


def parse_time(text: str, anchor: date,
               launch_date: date | None = None) -> tuple[int, int] | None:
    """返回 (start_store_day, end_store_day)；无法解析返回 None（调用方决定默认值）。

    Args:
        text: 用户问题文本
        anchor: 数仓数据最新日期（T+1 锚点）
        launch_date: 业务上线日期（用于解析"公测/上线/首发"锚点）
    """
    t = text.replace(" ", "")
    today = anchor

    # ---- 业务锚点：公测/上线/首发 → launch_date ----
    # "公测首周" / "公测首月" / "上线首周" / "公测前7天"
    launch_anchor = None
    if launch_date and re.search(r"公测|首发|上线|开服", t):
        launch_anchor = launch_date

    if launch_anchor:
        # "首周" / "第一个7天" / "前7天" → 上线日 ~ 上线日+6
        m = re.search(r"首周|前?7天|第一个?7天", t)
        if m:
            return _sd(launch_anchor), _sd(launch_anchor + timedelta(days=6))
        # "首月" / "第一个月" → 上线日 ~ 月末
        m = re.search(r"首月|第一个月|前?30天", t)
        if m:
            last_day = date(launch_anchor.year, launch_anchor.month,
                            monthrange(launch_anchor.year, launch_anchor.month)[1])
            return _sd(launch_anchor), _sd(min(last_day, today))
        # 只有"公测/上线"没说范围 → 默认当天
        return _sd(launch_anchor), _sd(min(launch_anchor, today))

    # ---- 相对时间：近N天 ----
    m = re.search(r"(?:近|最近|过去)(\d+)天", t)
    if m:
        n = min(int(m.group(1)), 180)
        return _sd(today - timedelta(days=n - 1)), _sd(today)
    if "今天" in t:
        return _sd(today), _sd(today)
    if "昨天" in t:
        return _sd(today - timedelta(days=1)), _sd(today - timedelta(days=1))
    if "上周" in t:
        monday = today - timedelta(days=today.weekday() + 7)
        return _sd(monday), _sd(monday + timedelta(days=6))
    if "本周" in t or "这周" in t:
        monday = today - timedelta(days=today.weekday())
        return _sd(monday), _sd(today)
    if "上个月" in t or "上月" in t:
        first = today.replace(day=1) - timedelta(days=1)
        return _sd(first.replace(day=1)), _sd(first)
    if "本月" in t or "这个月" in t:
        return _sd(today.replace(day=1)), _sd(today)

    # ---- 自然月：9月 / 2025年9月 ----
    m = re.search(r"(\d{4})年(\d{1,2})月", t)
    y, mo = (int(m.group(1)), int(m.group(2))) if m else (None, None)
    if m is None:
        m = re.search(r"(\d{1,2})月", t)
        if m:
            y, mo = today.year, int(m.group(1))
            if mo > today.month:          # "9月"而当前是10月 → 指今年9月（数仓有数据的最近的）
                y = today.year
    if m:
        last = date(y, mo, monthrange(y, mo)[1])
        start = date(y, mo, 1)
        return _sd(start), _sd(min(last, today))

    # ---- 具体日期：2025-09-01 / 2025-09-01至2025-09-07 ----
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[至到~\-]{1,2}(\d{4})-(\d{1,2})-(\d{1,2}))?", t)
    if m:
        d1 = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        d2 = date(int(m.group(4)), int(m.group(5)), int(m.group(6))) if m.group(4) else d1
        return _sd(d1), _sd(min(d2, today))

    return None


DEFAULT_RANGE_DAYS = 30


def default_range(anchor: date) -> tuple[int, int]:
    return _sd(anchor - timedelta(days=DEFAULT_RANGE_DAYS - 1)), _sd(anchor)
