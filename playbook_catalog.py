"""Presentation metadata for NBA 2K27 playbook CRCs."""
from __future__ import annotations

import re


POSITION_NAMES = ("控卫 PG", "分卫 SG", "小前 SF", "大前 PF", "中锋 C")
# Play type the game stores in bits 19-23 of each play record's type word.
# Types 3 and 4 are both screener-pop actions (their names use POP / OUT / FADE).
PLAY_TYPES = {
    1: ("单打", "单打"),
    2: ("挡拆", "挡拆持球"),
    3: ("挡拆", "挡拆外弹"),
    4: ("挡拆", "挡拆外弹"),
    5: ("挡拆", "挡拆顺下"),
    6: ("背身", "低位背身"),
    7: ("背身", "高位背身"),
    8: ("背身", "后卫背身"),
    9: ("空切", "空切"),
    10: ("手递手", "手递手"),
    11: ("投篮跑位", "中距离"),
    12: ("投篮跑位", "三分"),
}
GROUP_DETAILS = {
    "单打": ("单打",),
    "挡拆": ("挡拆持球", "挡拆外弹", "挡拆顺下"),
    "背身": ("低位背身", "高位背身", "后卫背身"),
    "空切": ("空接", "空切"),
    "手递手": ("手递手",),
    "投篮跑位": ("三分", "中距离"),
    "发球战术": ("三分", "中距离", "空接", "低位背身", "其他发球"),
    "其他": ("未识别",),
}
TYPE_ORDER = tuple(GROUP_DETAILS)
DETAIL_ORDER = tuple(dict.fromkeys(detail for details in GROUP_DETAILS.values() for detail in details))
ALLEY_OOP = re.compile(r"\b(ALLEY|LOB|OOP)\b", re.I)


def classify_play(name: str, word: int) -> dict:
    """Label a play from its game record; only the alley-oop mark comes from the name."""
    kind = (word >> 16) & 0xFF
    group, detail = PLAY_TYPES.get(kind >> 3, ("其他", "未识别"))
    inbound = kind & 7 == 3  # sideline / baseline inbound variant
    if ALLEY_OOP.search(name) and (group == "空切" or inbound):
        detail = "空接"
    if inbound:
        group = "发球战术"
        if detail == "未识别":
            detail = "其他发球"
    # Bits 9-11: focus player; bits 12-15: second player (screener / handoff partner).
    positions: list[int] = []
    for code in ((word >> 9) & 7, (word >> 12) & 0xF):
        if code < 5 and code + 1 not in positions:
            positions.append(code + 1)
    return {"group": group, "detail": detail, "positions": positions}
