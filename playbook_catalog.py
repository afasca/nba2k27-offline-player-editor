"""Presentation metadata for NBA 2K27 playbook CRCs."""
from __future__ import annotations

import re


POSITION_NAMES = ("控卫 PG", "分卫 SG", "小前 SF", "大前 PF", "中锋 C")
TYPE_CODES = {
    0x08: ("单打", "单打"),
    0x10: ("挡拆", "挡拆持球"),
    0x18: ("挡拆", "控卫挡拆"),
    0x20: ("挡拆", "侧翼挡拆"),
    0x28: ("挡拆", "挡拆顺下"),
    0x30: ("背身", "低位背身"),
    0x38: ("背身", "高位背身"),
    0x40: ("背身", "后卫背身"),
    0x48: ("空切", "空切"),
    0x50: ("手递手", "手递手"),
    0x58: ("投篮跑位", "中距离"),
    0x60: ("投篮跑位", "三分"),
}
TYPE_ORDER = ("单打", "挡拆", "背身", "空切", "手递手", "投篮跑位", "其他")
KEYWORD_TYPE = {
    "ISO": "单打", "ISOLATION": "单打",
    "FIST": "挡拆", "PNR": "挡拆", "PICK": "挡拆",
    "PUNCH": "背身", "POST": "背身", "HIGH": "背身",
    "CUT": "空切", "CUTTER": "空切",
    "GIVE": "手递手", "HANDOFF": "手递手", "DHO": "手递手",
    "QUICK": "投篮跑位", "FLOPPY": "投篮跑位", "STAGGER": "投篮跑位",
}
KEYWORDS = re.compile(r"\b(" + "|".join(KEYWORD_TYPE) + r")\b", re.I)
EXPLICIT_POSITION = re.compile(r"\b(PG|SG|SF|PF|C)\b", re.I)


def classify_play(name: str, type_code: int | None = None,
                  meta: int | None = None) -> dict:
    """Return game-derived labels where known and name-based labels otherwise."""
    exact = TYPE_CODES.get(type_code)
    keyword = KEYWORDS.search(name)
    group = exact[0] if exact else (KEYWORD_TYPE[keyword.group().upper()] if keyword else "其他")
    detail = exact[1] if exact else group
    positions: set[int] = set()
    source = "游戏记录" if exact else "名称推断"
    if exact and meta is not None:
        pos_code = (meta >> 8) & 0xF
        if pos_code < 10:
            positions.add(pos_code // 2 + 1)
    if not positions:
        for match in EXPLICIT_POSITION.finditer(name):
            positions.add({"PG": 1, "SG": 2, "SF": 3, "PF": 4, "C": 5}[match.group().upper()])
        if keyword:
            tail = name[keyword.end():].split()[:4]
            for token in tail:
                if re.fullmatch(r"[1-5]{1,2}", token):
                    positions.update(int(char) for char in token)
                    break
    return {"group": group, "detail": detail,
            "positions": sorted(positions), "source": source}
