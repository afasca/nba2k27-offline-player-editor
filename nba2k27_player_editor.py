"""Local NBA 2K27 roster editor for the inspected offline game build.

The program edits the loaded roster in memory. It does not patch game code.
"""
from __future__ import annotations

import ctypes as ct
from datetime import date
import json
import multiprocessing as mp
from pathlib import Path
import re
import struct
import sys
import time
import tkinter as tk
import traceback
from tkinter import messagebox, ttk
from collections import Counter
from decimal import Decimal, InvalidOperation, ROUND_FLOOR

import psutil
from playbook_catalog import DETAIL_ORDER, GROUP_DETAILS, POSITION_NAMES, TYPE_ORDER, classify_play
from team_badges import BadgeFactory, colors_from_record, fallback_colors, mix, tier_color
import ui_theme as theme
from ui_theme import P, ScrollFrame, SearchBox


# Offsets below were verified on this game build (PE timestamp, SizeOfImage).
# They are relative to the game module, so the install folder does not matter.
VERIFIED_BUILD = (0x6A8DF1C6, 0x35A63000)
ROOT_RVA = 161861320
PLAYER_STRIDE = 1272
PLAYBOOK_STRIDE = 536
PLAYBOOK_SLOTS = 88
PLAYBOOK_EDITABLE_SLOTS = 80
BODY_RATIO = Decimal("1.3")
BODY_MIN_CM = Decimal("50")
BODY_MAX_CM = Decimal("327.67")
RATING_MIN = 25
RATING_MAX = 125
FIELD_FILE = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "player_fields.json"
EXTRA_FILE = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "player_extra_fields.json"
ADVANCED_FILE = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "player_advanced_fields.json"
APPEARANCE_FILE = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "player_appearance_fields.json"
SIGNATURE_OPTIONS_FILE = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "player_signature_options.json"
PLAYBOOK_PLAYS_FILE = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "playbook_plays.json"
STAFF_FIELDS_FILE = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "staff_fields.json"
BACKUP_DIR = Path.home() / "Documents" / "NBA2K27_PlayerEditor_Backups"
SETTINGS_FILE = BACKUP_DIR / "editor_settings.json"
PRESET_FILE = BACKUP_DIR / "presets.json"
LOG_FILE = BACKUP_DIR / "editor.log"
FREE_AGENT = "自由球员"
# Primary/secondary RGBA colours and short name in the team record (this build).
TEAM_COLOR_OFFSET = 4940
TEAM_ABBR_OFFSET = 848
STAFF_TEAM_ARRAY_OFFSET = 3952
STAFF_TEAM_SLOTS = 32
STAFF_STRIDE = 456
STAFF_JOB_OFFSET = 386
STAFF_JOB_SHIFT = 0
STAFF_JOB_BITS = 5
STAFF_TARGET_JOB_OFFSET = 332
STAFF_TARGET_JOB_SHIFT = 25
STAFF_FIRST_OFFSET = 80
STAFF_LAST_OFFSET = 120
STAFF_UID_OFFSET = 288
STAFF_TEAM_OFFSET = 24
STAFF_STATUS_OFFSET = 383
STAFF_ATTRIBUTE_FIELDS = (
    ("motivation", "动力", 358), ("basketball_iq", "篮球智商", 360),
    ("work_ethic", "职业道德", 361), ("offense", "进攻教练", 362),
    ("defense", "防守教练", 363), ("business", "商业", 364),
    ("training", "训练", 365), ("charisma", "魅力", 366),
)
STAFF_JOB_NAMES = {
    0: "主教练", 1: "首席球探", 2: "队医", 3: "总经理", 4: "首席财务官",
    5: "老板", 6: "助理主教练", 7: "助理总经理", 8: "投篮教练",
    9: "后卫教练", 10: "侧翼教练", 11: "内线教练", 12: "低位防守教练",
    13: "外线防守教练", 14: "国内球探", 15: "国内球探", 16: "未使用",
    17: "未使用", 18: "未使用", 19: "国际球探", 20: "力量训练师",
    21: "体能训练师", 22: "运动心理师", 23: "运动科学师", 24: "理疗师",
    25: "睡眠医生",
}

LEAGUE_ORDER = ("NBA", "WNBA", "G 联盟", "国家队", "其他联赛", "自由球员")
WNBA_TEAMS = frozenset({
    "Atlanta Dream", "Chicago Sky", "Connecticut Sun", "Indiana Fever", "New York Liberty",
    "Toronto Tempo", "Washington Mystics", "Dallas Wings", "Golden State Valkyries",
    "Las Vegas Aces", "Los Angeles Sparks", "Minnesota Lynx", "Phoenix Mercury",
    "Portland Fire", "Seattle Storm",
})


def classify_league(team: str, roster_type: int | None) -> str:
    if team == "自由球员":
        return "自由球员"
    if team in WNBA_TEAMS:
        return "WNBA"
    return {0: "NBA", 18: "G 联盟", 22: "国家队"}.get(roster_type, "其他联赛")

PROCESS_VM_READ = 0x10
PROCESS_VM_WRITE = 0x20
PROCESS_VM_OPERATION = 0x08
PROCESS_QUERY_INFORMATION = 0x400
TH32CS_SNAPMODULE = 0x08
TH32CS_SNAPMODULE32 = 0x10
INVALID_HANDLE_VALUE = ct.c_void_p(-1).value

k32 = ct.WinDLL("kernel32", use_last_error=True)
k32.OpenProcess.argtypes = [ct.c_uint32, ct.c_int, ct.c_uint32]
k32.OpenProcess.restype = ct.c_void_p
k32.CloseHandle.argtypes = [ct.c_void_p]
k32.ReadProcessMemory.argtypes = [ct.c_void_p, ct.c_void_p, ct.c_void_p, ct.c_size_t, ct.POINTER(ct.c_size_t)]
k32.ReadProcessMemory.restype = ct.c_int
k32.WriteProcessMemory.argtypes = [ct.c_void_p, ct.c_void_p, ct.c_void_p, ct.c_size_t, ct.POINTER(ct.c_size_t)]
k32.WriteProcessMemory.restype = ct.c_int
k32.CreateToolhelp32Snapshot.argtypes = [ct.c_uint32, ct.c_uint32]
k32.CreateToolhelp32Snapshot.restype = ct.c_void_p


class MODULEENTRY32W(ct.Structure):
    _fields_ = [
        ("dwSize", ct.c_uint32), ("th32ModuleID", ct.c_uint32),
        ("th32ProcessID", ct.c_uint32), ("GlblcntUsage", ct.c_uint32),
        ("ProccntUsage", ct.c_uint32), ("modBaseAddr", ct.c_void_p),
        ("modBaseSize", ct.c_uint32), ("hModule", ct.c_void_p),
        ("szModule", ct.c_wchar * 256), ("szExePath", ct.c_wchar * 260),
    ]


k32.Module32FirstW.argtypes = [ct.c_void_p, ct.POINTER(MODULEENTRY32W)]
k32.Module32FirstW.restype = ct.c_int
k32.Module32NextW.argtypes = [ct.c_void_p, ct.POINTER(MODULEENTRY32W)]
k32.Module32NextW.restype = ct.c_int
k32.VirtualQueryEx.argtypes = [ct.c_void_p, ct.c_void_p, ct.c_void_p, ct.c_size_t]
k32.VirtualQueryEx.restype = ct.c_size_t
user32 = ct.WinDLL("user32", use_last_error=True)


def set_dpi_awareness():
    """Match window coordinates to real pixels so screen crops land on the game."""
    try:
        user32.SetProcessDpiAwarenessContext(ct.c_void_p(-4))  # per-monitor v2
    except (AttributeError, OSError):
        try:
            ct.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass


def log_error(context: str, exc: BaseException | None = None):
    """Windowed builds have no console; keep a small log next to the backups."""
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        detail = "".join(traceback.format_exception(exc)) if exc else ""
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {context}\n{detail}\n")
    except OSError:
        pass


def load_settings() -> dict:
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(data: dict):
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


PRESET_KINDS = ("signatures", "playbooks")


def load_presets() -> dict:
    """Saved 动作 / 战术手册 presets. A damaged file is set aside, never overwritten."""
    data = {}
    try:
        data = json.loads(PRESET_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        pass
    except ValueError as exc:
        log_error("预设文件已损坏，已改名保留并重新开始", exc)
        try:
            PRESET_FILE.replace(PRESET_FILE.with_name(f"presets.damaged-{time.strftime('%Y%m%d_%H%M%S')}.json"))
        except OSError:
            pass
    except OSError as exc:
        log_error("预设文件无法读取", exc)
    if not isinstance(data, dict):
        data = {}
    for kind in PRESET_KINDS:
        if not isinstance(data.get(kind), dict):
            data[kind] = {}
    return data


def save_presets(data: dict):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    temp = PRESET_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(PRESET_FILE)


BADGE_LEVELS = ("未装备", "铜", "银", "金", "名人堂", "传奇")
HOT_ZONE_LEVELS = ("冷区", "普通", "热区", "极热")


def extra_choices(field: dict) -> tuple[str, ...] | None:
    """Named values for badge tiers (0–5) and hot zones (0–3)."""
    if field["section"] == "Badges" and field["bits"] == 3:
        return tuple(f"{value} · {name}" for value, name in enumerate(BADGE_LEVELS))
    if field["section"] == "Tendencies" and field["bits"] == 2:
        return tuple(f"{value} · {name}" for value, name in enumerate(HOT_ZONE_LEVELS))
    return None


def extra_display(field: dict, value: int) -> str:
    choices = extra_choices(field)
    return choices[value] if choices and 0 <= value < len(choices) else str(value)


def leading_int(text: str) -> int:
    match = re.match(r"\s*(-?\d+)", text)
    if not match:
        raise ValueError("请输入数字")
    return int(match.group(1))


class MEMORY_BASIC_INFORMATION(ct.Structure):
    _fields_ = [
        ("BaseAddress", ct.c_void_p), ("AllocationBase", ct.c_void_p),
        ("AllocationProtect", ct.c_uint32), ("PartitionId", ct.c_uint16),
        ("RegionSize", ct.c_size_t), ("State", ct.c_uint32),
        ("Protect", ct.c_uint32), ("Type", ct.c_uint32),
    ]


def find_game() -> psutil.Process:
    """Find the running game by process name, wherever it is installed."""
    for proc in psutil.process_iter(["name"]):
        try:
            if (proc.info.get("name") or "").lower() == "nba2k27.exe":
                return proc
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    raise RuntimeError("没有找到正在运行的 NBA 2K27（NBA2K27.exe），请先启动游戏。")


def module_base(pid: int) -> int:
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
    if not snap or snap == INVALID_HANDLE_VALUE:
        raise OSError(ct.get_last_error(), "无法获取游戏模块")
    entry = MODULEENTRY32W()
    entry.dwSize = ct.sizeof(entry)
    try:
        ok = k32.Module32FirstW(snap, ct.byref(entry))
        while ok:
            if entry.szModule.lower() == "nba2k27.exe":
                return int(entry.modBaseAddr)
            ok = k32.Module32NextW(snap, ct.byref(entry))
    finally:
        k32.CloseHandle(snap)
    raise RuntimeError("找不到 NBA2K27.exe 主模块")


def decode_name(data: bytes) -> str:
    text = data.decode("utf-16le", errors="ignore").split("\0", 1)[0].strip()
    return text if text and all(c.isprintable() for c in text) else ""


def raw_to_rating(raw: int) -> int:
    return min(RATING_MAX, max(RATING_MIN, round(RATING_MIN + raw * 100 / 255)))


def rating_to_raw(rating: int) -> int:
    return max(0, min(255, round((rating - RATING_MIN) * 255 / 100)))


POSITIONS = ("控球后卫", "得分后卫", "小前锋", "大前锋", "中锋", "无")
HANDS = ("左手", "右手")
DUNK_HANDS = ("左手", "右手", "双手", "其他")
PROFILE_BITS = {
    "birth_year": (272, 16, 12, 1900, 2100),
    "birth_month": (874, 12, 4, 1, 12),
    "birth_day": (276, 1, 5, 1, 31),
    "jersey_number": (276, 11, 7, 0, 99),
    "position": (300, 8, 3, 0, 5),
    "secondary_position": (300, 11, 3, 0, 5),
    "dominant_hand": (300, 22, 1, 0, 1),
    "dunk_hand": (300, 23, 2, 0, 3),
    "draft_round": (856, 27, 4, 0, 15),
    "draft_pick": (874, 6, 6, 0, 63),
    "peak_start": (276, 18, 6, 0, 63),
    "peak_end": (276, 24, 6, 0, 63),
    "playing_time": (300, 15, 7, 0, 127),
    "years_with_team": (880, 0, 5, 0, 31),
}
PROFILE_UI = (
    ("first_name", "名"), ("last_name", "姓"), ("nickname", "昵称"),
    ("jersey_nickname", "球衣昵称"), ("jersey_number", "球衣号码"),
    ("position", "主位置"), ("secondary_position", "副位置"),
    ("weight_kg", "体重（公斤）"), ("birth_year", "出生年份"),
    ("birth_month", "出生月份"), ("birth_day", "出生日期"),
    ("dominant_hand", "惯用手"), ("dunk_hand", "扣篮惯用手"),
    ("draft_round", "选秀轮次"), ("draft_pick", "选秀顺位"),
    ("peak_start", "巅峰起始年龄"), ("peak_end", "巅峰结束年龄"),
    ("playing_time", "预期出场时间"), ("years_with_team", "效力本队年数"),
)

SIGNATURE_NAMES = {
    "ANIMATIONBLENDING": "投篮动作混合", "RELEASETIMING": "出手时机", "SHOTJUMPER": "跳投底座",
    "SHOTTIMINGIMPACTMODIFIER": "时机影响", "UPPERRELEASE1": "上半身出手一",
    "UPPERRELEASE2": "上半身出手二", "DRIBBLEPULLUP": "运球急停跳投",
    "FREETHROW": "罚球动作", "GOTOSHOT": "招牌投篮", "SHOTSIDEHOP": "侧跳投",
    "SHOTSPIN": "转身投篮", "SHOTSTEPTHRU": "跨步投篮", "LAYUPPACKAGE": "上篮动作包",
    "POSTFADE": "背身后仰", "GOPOSTTOSHOT": "背身接投篮", "POSTHOOK": "背身勾手",
    "POSTONESTEPPULLUP": "背身单步急停", "DRIBBLESTYLE": "运球风格",
    "ISOBEHINDBACK": "背后运球", "ISOBEHINDBACKLAUNCH": "背后运球启动",
    "ISOBREAKDOWN": "单打变向", "ISOBREAKDOWNMOVE": "单打变向动作",
    "ISOCOMBODOUBLECROSS": "连续双变向", "ISOCOMBOHESICROSS": "犹豫接变向",
    "ISOCROSSCOMBO": "交叉运球组合", "ISOCROSSOVER": "交叉运球",
    "ISOCROSSSPIN": "交叉接转身", "ISOESCAPEBEHINDBACK": "撤步背后运球",
    "ISOESCAPECROSS": "撤步交叉运球", "ISOESCAPEHES": "撤步犹豫步",
    "ISOESCAPETWEEN": "撤步胯下运球", "ISOHESITATION": "犹豫步",
    "ISOHESITATIONLATERAL": "侧向犹豫步", "ISOINANDOUT": "内外运球",
    "ISOMISDIRECTBEHINDBACK": "反向背后运球", "ISOMISDIRECTCROSS": "反向交叉运球",
    "ISOMISDIRECTHES": "反向犹豫步", "ISOSIZEUPSIG": "招牌花式运球",
    "ISOSPIN": "单打转身", "ISOSTEPBACK": "单打后撤步",
    "ISOSTEPBACKCROSS": "后撤步交叉运球", "ISOSTEPBACKLATERAL": "侧向后撤步",
    "ISOTWEENLEGS": "胯下运球", "PASSSTYLE": "传球风格",
    "TRIPLETHREATSTYLE": "三威胁风格", "TRIPLETHREATBREAKDOWN": "三威胁变向",
    "TRIPLETHREATJAB": "三威胁试探步", "TRIPLETHREATSTEPOVER": "三威胁跨步",
    "AMBIENTPACKAGE1": "场边动作包一", "AMBIENTPACKAGE1WEIGHT": "场边动作一权重",
    "AMBIENTPACKAGE2": "场边动作包二", "AMBIENTPACKAGE2WEIGHT": "场边动作二权重",
    "AMBIENTPACKAGE3": "场边动作包三", "AMBIENTPACKAGE3WEIGHT": "场边动作三权重",
    "EMOTEDRB": "运球表情动作", "EMOTETT": "三威胁表情动作",
    "FREEFALLSTYLE": "倒地风格", "MOTIONSTYLE": "跑动风格",
    "SIGJUMPBALLSTAND": "争球站姿", "SIGPLAYERINTRO1": "入场动作一",
    "SIGPLAYERINTRO2": "入场动作二", "VOICETYPE": "声音类型",
}


SIGNATURE_GROUPS = {
    "Jump Shooting": "投篮", "Jump Shooting II": "花式投篮",
    "Layups And Dunks": "上篮扣篮", "Post Game": "背身",
    "Ball Handling": "控球", "Misc": "其他",
}
ALL_SIGNATURES = "全部动作"


def signature_label(field: dict) -> str:
    dunk = re.fullmatch(r"DUNKPACKAGE(\d+)", field["id"])
    return f"扣篮动作包 {dunk.group(1)}" if dunk else SIGNATURE_NAMES.get(field["id"], field["label"])


def signature_option_name(raw: str) -> str:
    acronyms = {"NBA", "WNBA", "VJ", "RJ", "PJ", "OG", "DJ", "JJ", "JR", "II", "III", "IV"}
    return " ".join(word if word in acronyms or word.isdigit() else word.capitalize()
                    for word in raw.split("_"))


def masked_copy_words(source: bytes, target: bytes, fields: list[dict]) -> dict[int, bytes]:
    """Copy only described scalar bits, preserving all other target data."""
    output = bytearray(target)
    touched = set()
    for field in fields:
        offset = field["offset"]
        if offset < 0 or offset + 4 > min(len(source), len(target)) or offset % 4:
            raise ValueError(f"无效的复制字段：{field['id']}")
        bits, shift = field["bits"], field["shift"]
        if not 1 <= bits <= 32 or shift < 0 or bits + shift > 32:
            raise ValueError(f"无效的复制位段：{field['id']}")
        mask = ((1 << bits) - 1) << shift
        source_word = struct.unpack_from("<I", source, offset)[0]
        target_word = struct.unpack_from("<I", output, offset)[0]
        struct.pack_into("<I", output, offset, (target_word & ~mask) | (source_word & mask))
        touched.add(offset)
    return {offset: bytes(output[offset:offset + 4]) for offset in touched
            if output[offset:offset + 4] != target[offset:offset + 4]}


def encode_name(text: str) -> bytes:
    text = text.strip()
    if not text or "\x00" in text:
        raise ValueError("姓名不能为空，也不能包含空字符")
    raw = text.encode("utf-16-le")
    if len(raw) > 38:
        raise ValueError("姓名每部分最多 19 个 UTF-16 字符")
    return raw.ljust(40, b"\x00")


def encode_optional_text(text: str) -> bytes:
    if "\x00" in text:
        raise ValueError("文本不能包含空字符")
    raw = text.strip().encode("utf-16-le")
    if len(raw) > 38:
        raise ValueError("昵称最多 19 个 UTF-16 字符")
    return raw.ljust(40, b"\x00")


class GameMemory:
    def __init__(self):
        self.process = find_game()
        self.pid = self.process.pid
        self.handle = k32.OpenProcess(PROCESS_VM_READ | PROCESS_VM_WRITE | PROCESS_VM_OPERATION | PROCESS_QUERY_INFORMATION, False, self.pid)
        if not self.handle:
            raise OSError(ct.get_last_error(), "无法打开游戏进程")
        self.base = module_base(self.pid)
        try:
            header = self.base + self.u32(self.base + 0x3C)
            self.build = (self.u32(header + 8), self.u32(header + 24 + 56))
        except (OSError, ValueError):
            self.build = (0, 0)
        self.build_verified = self.build == VERIFIED_BUILD
        self.refresh_players()

    def refresh_players(self):
        root = self.u64(self.base + ROOT_RVA)
        roster = self.u64(root + 168) if root else 0
        self.count = self.u32(roster + 16) if roster else 0
        self.table = self.u64(roster + 24) if roster else 0
        self.stats_count = self.u32(roster + 464) if roster else 0
        self.stats_table = self.u64(roster + 472) if roster else 0
        if not self.table or not 100 <= self.count <= 10000:
            self.close()
            raise RuntimeError(self._layout_error("球员表位置与已验证版本不符"))
        self.players = self._load_players()
        if len(self.players) < 100:
            self.close()
            raise RuntimeError(self._layout_error("球员表验证失败"))

    def _layout_error(self, what: str) -> str:
        if self.build_verified:
            return f"{what}，已停止读取。请先进入游戏的球员名单或编辑页面，再点「重新连接」。"
        stamp = time.strftime("%Y-%m-%d", time.gmtime(self.build[0])) if self.build[0] else "未知"
        return (f"{what}，已停止读取。当前游戏版本（构建日期 {stamp}）与本工具适配的版本"
                f"（{time.strftime('%Y-%m-%d', time.gmtime(VERIFIED_BUILD[0]))}）不同，内存结构可能已变化。")

    def close(self):
        if getattr(self, "handle", None):
            k32.CloseHandle(self.handle)
            self.handle = None

    def read(self, address: int, size: int) -> bytes:
        if not address or size < 0:
            raise ValueError("无效的内存读取地址")
        buf = ct.create_string_buffer(size)
        done = ct.c_size_t()
        if not k32.ReadProcessMemory(self.handle, ct.c_void_p(address), buf, size, ct.byref(done)) or done.value != size:
            raise OSError(ct.get_last_error(), f"无法读取游戏内存 0x{address:X}")
        return buf.raw

    def write_verified(self, address: int, value: bytes):
        buf = ct.create_string_buffer(value)
        done = ct.c_size_t()
        if not k32.WriteProcessMemory(self.handle, ct.c_void_p(address), buf, len(value), ct.byref(done)) or done.value != len(value):
            raise OSError(ct.get_last_error(), f"无法写入游戏内存 0x{address:X}")
        if self.read(address, len(value)) != value:
            raise RuntimeError("游戏没有保留刚写入的值")

    def u64(self, address: int) -> int:
        return struct.unpack("<Q", self.read(address, 8))[0]

    def u32(self, address: int) -> int:
        return struct.unpack("<I", self.read(address, 4))[0]

    def _load_players(self) -> list[dict]:
        data = self.read(self.table, self.count * PLAYER_STRIDE)
        stats = self.read(self.stats_table, self.stats_count * 64) if self.stats_table and 0 < self.stats_count < 100000 else b""
        free_agent = {"team": FREE_AGENT, "league": FREE_AGENT, "nick": "", "abbr": "FA",
                      "colors": ("#343a46", "#8b93a1"), "team_ptr": 0}
        team_details: dict[int, dict] = {}
        self.teams = {(FREE_AGENT, FREE_AGENT): free_agent}
        result = []
        for index in range(self.count):
            row = data[index * PLAYER_STRIDE:(index + 1) * PLAYER_STRIDE]
            last = decode_name(row[:40])
            first = decode_name(row[40:80])
            if first and last:
                team_ptr = struct.unpack_from("<Q", row, 96)[0]
                if team_ptr and team_ptr not in team_details:
                    try:
                        team_row = self.read(team_ptr + 762, 86)
                        nickname = team_row[:50].decode("utf-16-le", errors="ignore").split("\x00", 1)[0]
                        city = team_row[50:86].decode("utf-16-le", errors="ignore").split("\x00", 1)[0]
                        team_name = f"{city} {nickname}".strip() or "球队未知"
                        roster_type = (self.u32(team_ptr + 4668) >> 26) & 63
                        abbr = decode_name(self.read(team_ptr + TEAM_ABBR_OFFSET, 14))
                        colors = colors_from_record(self.read(team_ptr + TEAM_COLOR_OFFSET, 8))
                        info = {"team": team_name, "league": classify_league(team_name, roster_type),
                                "nick": nickname.strip(), "abbr": abbr.upper()[:4],
                                "colors": colors or fallback_colors(team_name), "team_ptr": team_ptr}
                    except OSError:
                        info = {"team": "球队未知", "league": "其他联赛", "nick": "", "abbr": "?",
                                "colors": fallback_colors("球队未知"), "team_ptr": team_ptr}
                    if not info["abbr"]:
                        info["abbr"] = "".join(word[0] for word in info["team"].split()[:3]).upper() or "?"
                    team_details[team_ptr] = info
                    self.teams.setdefault((info["league"], info["team"]), info)
                stat_id = struct.unpack_from("<H", row, 304)[0]
                overall = None
                if stats and stat_id < self.stats_count:
                    overall = (struct.unpack_from("<I", stats, stat_id * 64 + 60)[0] >> 21) & 0x7F
                    if not 25 <= overall <= 99:
                        overall = None
                info = team_details.get(team_ptr, free_agent) if team_ptr else free_agent
                result.append({"index": index, "name": f"{first} {last}",
                               "uid": struct.unpack_from("<H", row, 296)[0],
                               "address": self.table + index * PLAYER_STRIDE,
                               "team": info["team"], "league": info["league"], "overall": overall,
                               "team_nick": info["nick"], "team_abbr": info["abbr"]})
        return result

    def team_info(self, player: dict) -> dict:
        return self.teams.get((player["league"], player["team"])) or self.teams[(FREE_AGENT, FREE_AGENT)]

    def team_options(self) -> list[dict]:
        """Return teams whose live records expose the staff pointer array."""
        result = [info for info in self.teams.values() if info.get("team_ptr")]
        return sorted(result, key=lambda info: (LEAGUE_ORDER.index(info["league"])
                                                if info["league"] in LEAGUE_ORDER else 99,
                                                info["team"]))

    def team_staff(self, team: dict) -> list[dict]:
        """Read staff records referenced by one live team record."""
        team_ptr = int(team.get("team_ptr") or 0)
        if not team_ptr:
            return []
        row = self.read(team_ptr, STAFF_TEAM_ARRAY_OFFSET + STAFF_TEAM_SLOTS * 8)
        pointers = struct.unpack_from(f"<{STAFF_TEAM_SLOTS}Q", row, STAFF_TEAM_ARRAY_OFFSET)
        result, seen = [], set()
        for slot, address in enumerate(pointers):
            if not address or address in seen:
                continue
            seen.add(address)
            staff = self.read(address, STAFF_STRIDE)
            first = decode_name(staff[STAFF_FIRST_OFFSET:STAFF_FIRST_OFFSET + 40])
            last = decode_name(staff[STAFF_LAST_OFFSET:STAFF_LAST_OFFSET + 40])
            word = struct.unpack_from("<I", staff, STAFF_JOB_OFFSET)[0]
            target_word = struct.unpack_from("<I", staff, STAFF_TARGET_JOB_OFFSET)[0]
            job = (word >> STAFF_JOB_SHIFT) & ((1 << STAFF_JOB_BITS) - 1)
            target_job = (target_word >> STAFF_TARGET_JOB_SHIFT) & ((1 << STAFF_JOB_BITS) - 1)
            attributes = {key: staff[offset] for key, _label, offset in STAFF_ATTRIBUTE_FIELDS}
            result.append({"slot": slot, "address": address,
                           "uid": struct.unpack_from("<H", staff, STAFF_UID_OFFSET)[0],
                           "first_name": first, "last_name": last,
                           "name": f"{first} {last}".strip() or f"未命名员工 #{slot}",
                           "job": job, "target_job": target_job,
                           "team_ptr": struct.unpack_from("<Q", staff, STAFF_TEAM_OFFSET)[0],
                           "status": staff[STAFF_STATUS_OFFSET] & 3, "attributes": attributes})
        return result

    def apply_staff(self, staff: dict, changes: dict[int, bytes], *, label: str) -> Path:
        """Write a staff record after checking its UID and team association."""
        address = int(staff["address"])
        current = self.read(address, STAFF_STRIDE)
        if struct.unpack_from("<H", current, STAFF_UID_OFFSET)[0] != staff["uid"]:
            raise RuntimeError("员工记录已变化，请重新读取球队员工。")
        if staff.get("team_ptr") and struct.unpack_from("<Q", current, STAFF_TEAM_OFFSET)[0] != staff["team_ptr"]:
            raise RuntimeError("员工所属球队已变化，请重新读取球队员工。")
        absolute = {address + offset: value for offset, value in changes.items()}
        return self.apply_many(absolute, label=f"球队员工 {label}")

    def apply_staff_many(self, updates: list[tuple[dict, dict[int, bytes]]], *, label: str) -> Path:
        """Apply one or more staff edits in one backup/undo operation."""
        absolute: dict[int, bytes] = {}
        for staff, changes in updates:
            address = int(staff["address"])
            current = self.read(address, STAFF_STRIDE)
            if struct.unpack_from("<H", current, STAFF_UID_OFFSET)[0] != staff["uid"]:
                raise RuntimeError("有员工记录已变化，请重新读取后再批量修改。")
            if staff.get("team_ptr") and struct.unpack_from("<Q", current, STAFF_TEAM_OFFSET)[0] != staff["team_ptr"]:
                raise RuntimeError("有员工所属球队已变化，请重新读取后再批量修改。")
            for offset, value in changes.items():
                absolute[address + offset] = value
        if not absolute:
            raise ValueError("没有需要写入的员工变化")
        return self.apply_many(absolute, label=f"球队员工批量修改 · {label}")

    def refresh_playbooks(self):
        """Read the live roster's authored playbook array and play CRC slots."""
        root = self.u64(self.base + ROOT_RVA)
        roster = self.u64(root + 168) if root else 0
        count = self.u32(roster + 432) if roster else 0
        table = self.u64(roster + 440) if roster else 0
        if not table or not 20 <= count <= 500:
            raise RuntimeError("战术手册地址或数量与这版游戏不符，已停止读取。")
        data = self.read(table, count * PLAYBOOK_STRIDE)
        books = []
        pool = set()
        for index in range(count):
            row = data[index * PLAYBOOK_STRIDE:(index + 1) * PLAYBOOK_STRIDE]
            book_id = struct.unpack_from("<H", row, 66)[0]
            name = decode_name(row[:56]) or f"未命名手册 #{book_id}"
            slots = struct.unpack_from("<88I", row, 108)
            books.append({"index": index, "id": book_id, "name": name,
                          "address": table + index * PLAYBOOK_STRIDE, "slots": slots})
            pool.update(value for value in slots[:PLAYBOOK_EDITABLE_SLOTS] if value)
        if not 20 <= len(books) <= 500 or len(pool) < 100:
            raise RuntimeError("战术手册数据验证失败，已停止读取。")
        self.playbook_table = table
        self.playbooks = books
        self.play_crc_pool = pool

    def replace_playbook_slot(self, book: dict, slot: int, crc: int) -> Path:
        return self.edit_playbook_slots(book, {slot: crc})

    def edit_playbook_slots(self, book: dict, updates: dict[int, int], *, known: frozenset[int] = frozenset()) -> Path:
        """Validate and save one or more playbook slots as a single undoable edit.

        ``known`` adds plays verified in the game's play catalog but used by no
        playbook right now (e.g. restoring a preset after they were removed).
        """
        if not updates:
            raise ValueError("没有选择需要修改的战术槽位。")
        if any(not 0 <= slot < PLAYBOOK_EDITABLE_SLOTS for slot in updates):
            raise ValueError("只能修改前 80 个标准战术槽位。")
        self.refresh_playbooks()
        if not 0 <= book["index"] < len(self.playbooks):
            raise RuntimeError("战术手册列表已变化，请重新读取。")
        current = self.playbooks[book["index"]]
        if (current["id"], current["name"]) != (book["id"], book["name"]):
            raise RuntimeError("游戏已切换名单或战术手册，请重新选择手册。")
        changes = {}
        for slot, crc in updates.items():
            if current["slots"][slot] != book["slots"][slot]:
                raise RuntimeError(f"游戏中的第 {slot + 1} 个战术槽位已变化，请重新读取后再修改。")
            if crc and crc not in self.play_crc_pool and crc not in known:
                raise ValueError("该战术不在当前已载入的手册中，已停止写入。")
            if current["slots"][slot] != crc:
                changes[current["address"] + 108 + slot * 4] = struct.pack("<I", crc)
        if not changes:
            raise ValueError("所选战术槽位没有需要修改的变化。")
        return self.apply_many(changes, label=f"战术手册 {current['name']}：{len(changes)} 个槽位")

    def find_current_editor(self, *, deep: bool = False) -> tuple[dict, int] | None:
        """Locate the live player-editor panel for this executable build."""
        # The panel object's vtable identifies the active editor view. Its
        # player-record pointer is 0x28 bytes before that vtable pointer.
        marker = struct.pack("<Q", self.base + 0x68177C8)
        names = {}
        for player in self.players:
            names.setdefault((player["name"], player["uid"]), []).append(player)
        address = 0x10000
        info = MEMORY_BASIC_INFORMATION()
        regions: list[tuple[int, int]] = []
        while address < 0x7FFFFFFEFFFF:
            got = k32.VirtualQueryEx(self.handle, ct.c_void_p(address), ct.byref(info), ct.sizeof(info))
            if not got:
                address += 0x1000
                continue
            region = int(info.BaseAddress or 0)
            size = int(info.RegionSize)
            if size <= 0:
                address += 0x1000
                continue
            if info.State == 0x1000 and info.Type == 0x20000 and info.Protect & 0xFF in (0x04, 0x08, 0x40, 0x80):
                regions.append((region, size))
            address = region + size

        def scan(pool: list[tuple[int, int]]) -> list[tuple[int, dict, int]]:
            candidates: list[tuple[int, dict, int]] = []
            for region, size in pool:
                for off in range(0, size, 16 * 1024 * 1024):
                    start = region + off
                    length = min(16 * 1024 * 1024, size - off)
                    try:
                        data = self.read(start, length)
                    except OSError:
                        continue
                    pos = data.find(marker)
                    while pos >= 0:
                        found = start + pos
                        if found % 8 == 0:
                            try:
                                record = self.u64(found - 0x28)
                                row = self.read(record, 128)
                                name = f"{decode_name(row[40:80])} {decode_name(row[:40])}"
                                uid = struct.unpack("<H", self.read(record + 296, 2))[0]
                                matches = names.get((name, uid), [])
                                if len(matches) == 1 and self.u64(record + 120):
                                    candidates.append((found, matches[0], record))
                            except (OSError, ValueError):
                                pass
                        pos = data.find(marker, pos + 8)
            return candidates

        # The editor's small UI allocations sit near the active roster table
        # in this build. Search those first; the full scan remains a fallback.
        near = [(region, size) for region, size in regions
                if self.table - 4 * 1024**3 <= region < self.table and size <= 4 * 1024**2]
        candidates = scan(near)
        if deep and not candidates:
            near_set = set(near)
            candidates += scan([region for region in regions if region not in near_set])
        if not candidates:
            return None
        # Game keeps older editor panel copies in the same heap; the latest
        # panel allocation is the active editor without consulting the screen.
        _panel, player, record = max(candidates, key=lambda item: item[0])
        return player, record

    def player_snapshot(self, player: dict, fields: list[dict], *, record_address: int | None = None,
                        extra_fields: list[dict] | None = None,
                        signature_fields: list[dict] | None = None) -> dict:
        row = self.read(record_address or player["address"], PLAYER_STRIDE)
        name = f"{decode_name(row[40:80])} {decode_name(row[:40])}"
        uid = struct.unpack_from("<H", row, 296)[0]
        if name != player["name"] or uid != player["uid"]:
            raise RuntimeError("球员名单已经变化，请重新连接游戏。")
        appearance = struct.unpack_from("<Q", row, 120)[0]
        body = self.read(appearance, 52)
        height, wingspan = struct.unpack_from("<HH", body)
        arm_scale = struct.unpack_from("<f", body, 12)[0]
        custom_scales = bool(struct.unpack_from("<I", body, 48)[0] & (1 << 30))
        values = {field["id"]: raw_to_rating(row[field["offset"]]) for field in fields}
        profile = {
            "last_name": decode_name(row[:40]), "first_name": decode_name(row[40:80]),
            "nickname": decode_name(row[544:584]), "jersey_nickname": decode_name(row[584:624]),
            "weight_kg": round(struct.unpack_from("<f", row, 264)[0] / 2.2046226218, 1),
        }
        for key, (offset, shift, bits, _low, _high) in PROFILE_BITS.items():
            profile[key] = (struct.unpack_from("<I", row, offset)[0] >> shift) & ((1 << bits) - 1)
        extras = {field["section"] + ":" + field["id"]:
                  (struct.unpack_from("<I", row, field["offset"])[0] >> field["shift"]) & ((1 << field["bits"]) - 1)
                  for field in (extra_fields or [])}
        signatures = {field["id"]: (struct.unpack_from("<I", row, field["offset"])[0] >> field["shift"])
                      & ((1 << field["bits"]) - 1) for field in (signature_fields or [])}
        return {"height_cm": round((height & 0x7FFF) / 100, 2), "wingspan_cm": round((wingspan & 0x7FFF) / 100, 2),
                "arm_scale": round(arm_scale, 4), "custom_scales": custom_scales,
                "appearance": appearance, "ratings": values, "extras": extras,
                "signatures": signatures, "profile": profile}

    def apply(self, player: dict, changes: dict[int, bytes], appearance_changes: dict[int, bytes], *,
              edit_record: int | None = None, masks: dict[int, int] | None = None) -> Path:
        # Verify identity and every original value before writing. The backup lets
        # this run be reverted even if a later write fails.
        self.player_snapshot(player, [])
        changes_full = {player["address"] + offset: value for offset, value in changes.items()}
        appearance = self.u64(player["address"] + 120)
        changes_full.update({appearance + offset: value for offset, value in appearance_changes.items()})
        if edit_record and edit_record != player["address"]:
            self.player_snapshot(player, [], record_address=edit_record)
            for offset, value in changes.items():
                if masks and offset in masks:
                    old_word = self.u32(edit_record + offset)
                    new_word = struct.unpack("<I", value)[0]
                    mask = masks[offset]
                    value = struct.pack("<I", (old_word & ~mask) | (new_word & mask))
                changes_full[edit_record + offset] = value
            edit_appearance = self.u64(edit_record + 120)
            for offset, value in appearance_changes.items():
                if offset in (0, 2) and len(value) == 2:
                    original = struct.unpack("<H", self.read(edit_appearance + offset, 2))[0]
                    new = struct.unpack("<H", value)[0]
                    value = struct.pack("<H", (original & 0x8000) | (new & 0x7FFF))
                elif offset == 48 and len(value) == 4:
                    original = self.u32(edit_appearance + offset)
                    new = struct.unpack("<I", value)[0]
                    mask = 1 << 30
                    value = struct.pack("<I", (original & ~mask) | (new & mask))
                changes_full[edit_appearance + offset] = value
        old = {address: self.read(address, len(value)) for address, value in changes_full.items()}
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        backup = BACKUP_DIR / f"{stamp}_{self.pid}_{player['index']}.json"
        backup.write_text(json.dumps({
            "pid": self.pid, "player": player["name"], "index": player["index"],
            "values": [{"address": address, "before": value.hex(), "after": changes_full[address].hex()}
                       for address, value in old.items()],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        written = []
        try:
            for address, value in changes_full.items():
                self.write_verified(address, value)
                written.append(address)
        except Exception:
            for address in reversed(written):
                try:
                    self.write_verified(address, old[address])
                except Exception:
                    pass
            raise
        return backup

    def undo(self, backup: Path):
        saved = json.loads(backup.read_text(encoding="utf-8"))
        if saved["pid"] != self.pid:
            raise RuntimeError("游戏已重启，不能按旧内存地址撤销。")
        entries = saved["values"]
        for entry in entries:
            address = int(entry["address"])
            expected = bytes.fromhex(entry["after"])
            if self.read(address, len(expected)) != expected:
                raise RuntimeError("游戏中的值已有新变化，已停止撤销以免覆盖。")
        for entry in reversed(entries):
            self.write_verified(int(entry["address"]), bytes.fromhex(entry["before"]))

    def apply_many(self, changes: dict[int, bytes], *, label: str) -> Path:
        if not changes:
            raise ValueError("目标球员已有相同的 DNA，没有需要写入的变化")
        old = {address: self.read(address, len(value)) for address, value in changes.items()}
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        backup = BACKUP_DIR / f"{time.strftime('%Y%m%d_%H%M%S')}_{time.time_ns()}_{self.pid}_dna.json"
        backup.write_text(json.dumps({
            "pid": self.pid, "player": label,
            "values": [{"address": address, "before": value.hex(), "after": changes[address].hex()}
                       for address, value in old.items()],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        written = []
        try:
            for address, value in changes.items():
                self.write_verified(address, value)
                written.append(address)
        except Exception:
            for address in reversed(written):
                try:
                    self.write_verified(address, old[address])
                except Exception:
                    pass
            raise
        return backup

    def copy_dna(self, source: dict, targets: list[dict], kind: str, *,
                 advanced_fields: list[dict], extra_fields: list[dict],
                 appearance_fields: list[dict], source_record: int | None = None,
                 dry_run: bool = False) -> Path | dict[int, bytes]:
        if kind not in ("data", "appearance") or not targets:
            raise ValueError("请选择 DNA 类型和目标球员")
        self.player_snapshot(source, [], record_address=source_record)
        source_row = self.read(source_record or source["address"], PLAYER_STRIDE)
        targets = [player for player in targets if player["index"] != source["index"]]
        if not targets:
            raise ValueError("目标中没有其他球员")

        changes: dict[int, bytes] = {}
        if kind == "data":
            fields = [field for field in advanced_fields
                      if field["section"] == "Signature" or
                      (field["section"] == "Attributes" and not field["id"].startswith("CACHED"))]
            fields.extend(extra_fields)
        else:
            fields = [field for field in advanced_fields if field["section"] in ("Gear", "Appearance")]
            fields.extend([
                {"id": "WEIGHT", "offset": 264, "bits": 32, "shift": 0},
                {"id": "HAIRLENGTH", "offset": 280, "bits": 8, "shift": 10},
                {"id": "FACETYPE", "offset": 284, "bits": 2, "shift": 3},
                {"id": "SKINTYPE", "offset": 476, "bits": 3, "shift": 0},
            ])
            source_appearance = struct.unpack_from("<Q", source_row, 120)[0]
            source_head = struct.unpack_from("<Q", source_row, 112)[0]
            if not source_appearance or not source_head:
                raise RuntimeError("来源球员缺少外貌数据")
            appearance_source = self.read(source_appearance, 104)
            head_source = self.read(source_head, 60)
            appearance_parts = [field for field in appearance_fields if field["parent"] == "Appearance Data"]
            head_parts = [field for field in appearance_fields if field["parent"] == "Head Pointer"]
            appearance_parts.extend([
                {"id": "HEIGHT", "offset": 0, "bits": 15, "shift": 0},
                {"id": "WINGSPAN", "offset": 0, "bits": 15, "shift": 16},
                {"id": "TRUNK_SCALE", "offset": 4, "bits": 32, "shift": 0},
                {"id": "SHOULDER_SCALE", "offset": 8, "bits": 32, "shift": 0},
                {"id": "ARM_SCALE", "offset": 12, "bits": 32, "shift": 0},
                {"id": "NECK_SCALE", "offset": 16, "bits": 32, "shift": 0},
                {"id": "LOWER_SCALE", "offset": 20, "bits": 32, "shift": 0},
                {"id": "HAND_SCALE", "offset": 24, "bits": 32, "shift": 0},
            ])
            roster = self.read(self.table, self.count * PLAYER_STRIDE)
            appearance_refs = Counter(struct.unpack_from("<Q", roster, i * PLAYER_STRIDE + 120)[0]
                                      for i in range(self.count))
            head_refs = Counter(struct.unpack_from("<Q", roster, i * PLAYER_STRIDE + 112)[0]
                                for i in range(self.count))

        for player in targets:
            self.player_snapshot(player, [])
            target_row = self.read(player["address"], PLAYER_STRIDE)
            for offset, value in masked_copy_words(source_row, target_row, fields).items():
                changes[player["address"] + offset] = value
            if kind == "appearance":
                target_appearance = struct.unpack_from("<Q", target_row, 120)[0]
                target_head = struct.unpack_from("<Q", target_row, 112)[0]
                if (not target_appearance or not target_head or
                        appearance_refs[target_appearance] != 1 or head_refs[target_head] != 1):
                    raise RuntimeError(f"{player['name']} 的外貌数据被共享或缺失，已停止复制")
                body = self.read(target_appearance, 104)
                head = self.read(target_head, 60)
                for offset, value in masked_copy_words(appearance_source, body, appearance_parts).items():
                    changes[target_appearance + offset] = value
                for offset, value in masked_copy_words(head_source, head, head_parts).items():
                    changes[target_head + offset] = value
        if dry_run:
            return changes
        return self.apply_many(changes, label=f"{source['name']} {kind} DNA → {len(targets)} 名球员")


def detect_current_worker(sender, deep: bool):
    """Find the active editor panel from the game's memory outside Tk."""
    set_dpi_awareness()
    memory = None
    try:
        memory = GameMemory()
        found = memory.find_current_editor(deep=deep)
        message = {"pid": memory.pid, "method": "memory"}
        if found:
            message.update(index=found[0]["index"], uid=found[0]["uid"], address=found[1])
        sender.send(message)
    except Exception as exc:
        log_error("current-player worker", exc)
        sender.send({"error": str(exc)})
    finally:
        if memory:
            memory.close()
        sender.close()


APP_TITLE = "NBA 2K27 球员与战术修改器"
RATING_GROUPS = {"Offense": "进攻", "Defense": "防守", "Athleticism": "运动",
                 "Durability": "耐久", "Mental": "意识", "Misc": "其他"}


class PlayerEditor(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.style = theme.apply_theme(self)
        theme.dark_title_bar(self)
        self.badges = BadgeFactory(self)
        self.settings = load_settings()
        self.geometry(self.settings.get("geometry") or "1360x880")
        self.minsize(1100, 700)
        if self.settings.get("zoomed"):
            self.state("zoomed")
        self.fields = json.loads(FIELD_FILE.read_text(encoding="utf-8"))
        self.extra_fields = json.loads(EXTRA_FILE.read_text(encoding="utf-8"))
        self.advanced_fields = json.loads(ADVANCED_FILE.read_text(encoding="utf-8"))
        self.appearance_fields = json.loads(APPEARANCE_FILE.read_text(encoding="utf-8"))
        self.signature_options = json.loads(SIGNATURE_OPTIONS_FILE.read_text(encoding="utf-8"))
        self.play_catalog = {int(key, 16): {"name": value["name"], **classify_play(value["name"], value["word"])}
                             for key, value in json.loads(PLAYBOOK_PLAYS_FILE.read_text(encoding="utf-8")).items()}
        self.staff_fields = json.loads(STAFF_FIELDS_FILE.read_text(encoding="utf-8"))
        self.signature_fields = [field for field in self.advanced_fields if field["section"] == "Signature"]
        self.memory: GameMemory | None = None
        self.selected: dict | None = None
        self.player_items: dict[str, dict] = {}
        self._tree_open_state: dict[str, bool] = {}
        self._last_search = ""
        self.rating_inputs: dict[str, tk.StringVar] = {}
        self.extra_inputs: dict[str, tk.StringVar] = {}
        self.signature_inputs: dict[str, tk.StringVar] = {}
        self.profile_inputs: dict[str, tk.StringVar] = {}
        self.baseline: dict | None = None
        self.current_edit_address: int | None = None
        self.last_backup: Path | None = None
        self.playbook_last_backup: Path | None = None
        self.staff_last_backup: Path | None = None
        self.staff_team_map: dict[str, dict] = {}
        self.staff_items: dict[str, dict] = {}
        self.staff_selected: dict | None = None
        self.book_option_map: dict[str, dict] = {}
        self.play_catalog_items: dict[str, int] = {}
        self.play_use_count: Counter[int] = Counter()
        self.detect_process = None
        self.detect_receiver = None
        self.detect_started = 0.0
        self.detect_memory = None
        self.detect_pending = False
        self.tracked: list[tuple] = []
        self._tab_titles: dict[tuple, str] = {}
        self.dirty_count = 0
        self._dirty_job = None
        self._search_job = None
        self.height = tk.StringVar()
        self.wingspan = tk.StringVar()
        self.arm_scale = tk.StringVar()
        self.custom_scales = tk.BooleanVar()
        self.status = tk.StringVar(value="正在连接游戏…")
        self.dirty_text = tk.StringVar()
        self.connection_text = tk.StringVar(value="未连接")
        self.count_text = tk.StringVar()
        self.busy_text = tk.StringVar()
        self.search = tk.StringVar()
        self.league_filter = tk.StringVar(value="全部联赛")
        self.team_filter = tk.StringVar(value="全部球队")
        self.book_choice = tk.StringVar()
        self.book_search = tk.StringVar()
        self.play_type_filter = tk.StringVar(value="全部打法")
        self.play_detail_filter = tk.StringVar(value="全部细分")
        self.play_position_filter = tk.StringVar(value="全部位置")
        self.play_search = tk.StringVar()
        self.playbook_status = tk.StringVar(value="打开此页后读取游戏战术手册")
        self.presets = load_presets()
        self.signature_preset_name = tk.StringVar()
        self.signature_preset_scope = tk.StringVar(value=ALL_SIGNATURES)
        self.signature_preset_info = tk.StringVar()
        self.playbook_preset_name = tk.StringVar()
        self.staff_team_choice = tk.StringVar()
        self.staff_first_name = tk.StringVar()
        self.staff_last_name = tk.StringVar()
        self.staff_job = tk.StringVar()
        self.staff_attribute_inputs: dict[str, tk.StringVar] = {}
        self.staff_field_search = tk.StringVar()
        self.staff_field_value = tk.StringVar()
        self.staff_field_selected: dict | None = None
        self.staff_field_items: dict[str, dict] = {}
        self.staff_status = tk.StringVar(value="选择球队后读取员工")
        self._make_ui()
        self._refresh_preset_boxes()
        self._bind_shortcuts()
        self.protocol("WM_DELETE_WINDOW", self._quit)
        self.after(200, self.connect)
        self.after(4000, self._watchdog)

    def report_callback_exception(self, exc, value, tb):
        """Tk swallows callback errors in windowed builds; log and show them instead."""
        log_error("界面操作出错", value)
        if self.detect_process is not None:
            self._finish_detect()
        try:
            messagebox.showerror("操作失败", f"{value}\n\n详细信息已记录到：\n{LOG_FILE}", parent=self)
        except tk.TclError:
            pass

    def _icon(self, name: str, color: str | None = None, size: int = 16):
        return self.badges.glyph(name, color or P["text"], size)

    def _make_ui(self):
        banner = tk.Frame(self, bg=P["banner"], padx=16, pady=7)
        banner.pack(fill="x")
        tk.Label(banner, image=self._icon("warning", "#ffffff", 18), bg=P["banner"]).pack(side="left")
        tk.Label(banner, text="必须离线运行游戏", bg=P["banner"], fg="#ffffff",
                 font=(theme.UI, 13, "bold")).pack(side="left", padx=(8, 14))
        tk.Label(banner, text="仅用于本地离线游戏。启动游戏前请断开网络。", bg=P["banner"], fg="#ffd9db",
                 font=theme.FONT).pack(side="left")

        header = ttk.Frame(self, style="Bar.TFrame", padding=(16, 10))
        header.pack(fill="x")
        tk.Label(header, text="NBA 2K27", bg=P["panel"], fg=P["accent"], font=(theme.DISPLAY, 17)).pack(side="left")
        tk.Label(header, text="球员与战术修改器", bg=P["panel"], fg=P["text"],
                 font=(theme.UI, 12, "bold")).pack(side="left", padx=(8, 22))
        self.connection_dot = tk.Label(header, text="●", bg=P["panel"], fg=P["danger"], font=(theme.UI, 11))
        self.connection_dot.pack(side="left")
        ttk.Label(header, textvariable=self.connection_text, style="Status.TLabel").pack(side="left", padx=(5, 0))
        self.reconnect_button = ttk.Button(header, text="重新连接", image=self._icon("link"), compound="left",
                                           style="Bar.TButton", command=self.connect)
        self.reconnect_button.pack(side="right")
        self.deep_button = ttk.Button(header, text="深度内存扫描", image=self._icon("scan"), compound="left",
                                      style="Bar.TButton", command=lambda: self.detect_current(deep=True))
        self.deep_button.pack(side="right", padx=8)
        self.detect_button = ttk.Button(header, text="内存识别当前球员", image=self._icon("scan", P["accent_text"]),
                                        compound="left", style="Accent.TButton", command=self.detect_current)
        self.detect_button.pack(side="right")
        self.cancel_detect_button = ttk.Button(header, text="取消", style="Bar.TButton", command=self._cancel_detect)
        self.busy_bar = ttk.Progressbar(header, mode="indeterminate", length=110,
                                        style="Accent.Horizontal.TProgressbar")
        self.busy_label = ttk.Label(header, textvariable=self.busy_text, style="Busy.TLabel")

        bottom = ttk.Frame(self, style="Bar.TFrame", padding=(16, 10))
        bottom.pack(side="bottom", fill="x")
        self.save_button = ttk.Button(bottom, text="保存修改", image=self._icon("save", P["accent_text"]),
                                      compound="left", style="Accent.TButton", command=self.save)
        self.save_button.pack(side="right")
        for text, icon, command in (("撤销上次保存", "undo", self.undo), ("重新读取", "refresh", self._reload_clicked),
                                    ("批量修改", "edit", self.batch_edit), ("复制 DNA", "copy", self.copy_dna_dialog)):
            ttk.Button(bottom, text=text, image=self._icon(icon), compound="left", style="Bar.TButton",
                       command=command).pack(side="right", padx=(0, 8))
        ttk.Label(bottom, textvariable=self.dirty_text, style="Pending.TLabel").pack(side="right", padx=(0, 16))
        ttk.Label(bottom, textvariable=self.status, style="Status.TLabel").pack(side="left", fill="x", expand=True)

        middle = ttk.PanedWindow(self, orient="horizontal")
        middle.pack(fill="both", expand=True, padx=12, pady=(10, 10))
        self.main_panes = middle
        # Fixed-width finder; the editor side takes all resizing.
        left = ttk.Frame(middle, style="Card.TFrame", padding=12, width=410)
        middle.add(left, weight=0)
        self.player_panel = left
        self._make_player_finder(left)
        left.pack_propagate(False)
        right = ttk.Frame(middle, padding=(12, 0, 0, 0))
        middle.add(right, weight=5)
        self._make_player_card(right)
        self.tabs = ttk.Notebook(right)
        self.tabs.pack(fill="both", expand=True, pady=(10, 0))
        self._make_profile_tab()
        self._make_staff_tab()
        self._make_body_tab()
        self._make_rating_tab()
        self._make_extra_tabs()
        self._make_signature_tab()
        self._make_playbook_tab()
        self._make_advanced_tab()
        self.tabs.bind("<<NotebookTabChanged>>", self._tab_changed)
        self._update_player_card()

    def _make_player_finder(self, left):
        ttk.Label(left, text="查找球员", style="CardSection.TLabel").pack(anchor="w")
        search = SearchBox(left, self.search, "搜索姓名、编号或球队…", icons=self._icon)
        search.pack(fill="x", pady=(6, 8))
        self.search_entry = search.entry
        self.search_entry.bind("<Return>", self._select_first_match)
        self.search.trace_add("write", lambda *_: self._schedule_filter())
        filters = ttk.Frame(left, style="Card.TFrame")
        filters.pack(fill="x")
        self.league_box = ttk.Combobox(filters, textvariable=self.league_filter, state="readonly", width=9,
                                       values=("全部联赛", *LEAGUE_ORDER))
        self.league_box.pack(side="left")
        self.league_box.bind("<<ComboboxSelected>>", lambda _event: self._filter())
        self.team_box = ttk.Combobox(filters, textvariable=self.team_filter, state="readonly", values=("全部球队",))
        self.team_box.pack(side="left", fill="x", expand=True, padx=(8, 0))
        self.team_box.bind("<<ComboboxSelected>>", lambda _event: self._filter())
        ttk.Label(left, textvariable=self.count_text, style="CardMuted.TLabel").pack(anchor="w", pady=(8, 4))
        player_frame = ttk.Frame(left, style="Card.TFrame")
        player_frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(player_frame, columns=("overall", "index"), show="tree headings", selectmode="browse")
        tree.heading("#0", text="联赛 / 球队 / 球员", anchor="w")
        tree.heading("overall", text="总评")
        tree.heading("index", text="编号")
        tree.column("#0", width=200, minwidth=160, stretch=True)
        tree.column("overall", width=50, minwidth=50, stretch=False, anchor="center")
        tree.column("index", width=56, minwidth=56, stretch=False, anchor="center")
        tree.tag_configure("league", font=theme.FONT_BOLD)
        tree.tag_configure("empty", foreground=P["faint"])
        vertical = ttk.Scrollbar(player_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vertical.set)
        tree.pack(side="left", fill="both", expand=True)
        vertical.pack(side="right", fill="y")
        tree.bind("<<TreeviewSelect>>", self._select)
        self.player_tree = tree
        ttk.Label(left, text="Ctrl+F 搜索 · Enter 选第一名 · Ctrl+D 识别 · Ctrl+S 保存 · F5 重新读取",
                  style="CardMuted.TLabel", font=theme.FONT_SMALL, wraplength=380).pack(anchor="w", pady=(8, 0))

    def _make_player_card(self, parent):
        card = ttk.Frame(parent, style="Card.TFrame", padding=(0, 0, 16, 0))
        card.pack(fill="x")
        self.card_stripe = tk.Frame(card, width=5, bg=P["border"])
        self.card_stripe.pack(side="left", fill="y")
        self.card_badge = ttk.Label(card, style="Card.TLabel")
        self.card_badge.pack(side="left", padx=(14, 12), pady=12)
        text = ttk.Frame(card, style="Card.TFrame")
        text.pack(side="left", fill="x", expand=True, pady=10)
        self.header = ttk.Label(text, style="CardName.TLabel")
        self.header.pack(anchor="w")
        self.card_meta = ttk.Label(text, style="CardMuted.TLabel")
        self.card_meta.pack(anchor="w", pady=(2, 0))
        self.card_rating = ttk.Label(card, style="CardMuted.TLabel", compound="top", font=theme.FONT_SMALL)
        self.card_rating.pack(side="right", pady=10)
        self.card_live = ttk.Label(card, style="CardMuted.TLabel", foreground=P["success"])
        self.card_live.pack(side="right", padx=14)

    def _update_player_card(self):
        player = self.selected
        if not player or not self.memory:
            self.card_stripe.configure(bg=P["border"])
            self.card_badge.configure(image=self.badges.disc("?", P["panel_hi"], P["border_hi"]))
            self.header.configure(text="请选择球员", font=(theme.UI, 20, "bold"))
            self.card_meta.configure(text="从左侧名单选择，或停在游戏的「编辑球员」页面后点「内存识别当前球员」。")
            self.card_rating.configure(image="", text="")
            self.card_live.configure(text="")
            return
        info = self.memory.team_info(player)
        primary, secondary = info["colors"]
        self.card_stripe.configure(bg=secondary if primary == "#000000" else primary)
        self.card_badge.configure(image=self.badges.disc(info["abbr"], primary, secondary))
        self.header.configure(text=player["name"], font=theme.FONT_NAME)
        parts = [player["team"], player["league"]]
        if self.baseline:
            profile = self.baseline["profile"]
            parts.append(self._profile_display("position", profile["position"]))
            parts.append(f"{self.baseline['height_cm']:g} 厘米")
            parts.append(f"球衣 {profile['jersey_number']} 号")
        parts.append(f"编号 #{player['index']}")
        self.card_meta.configure(text="   ·   ".join(parts))
        overall = player["overall"]
        self.card_rating.configure(image=self.badges.chip("—" if overall is None else str(overall),
                                                          tier_color(overall)), text="总评")
        self.card_live.configure(text="● 已关联游戏编辑器" if self.current_edit_address else "")

    def _make_body_tab(self):
        page = ttk.Frame(self.tabs, padding=20)
        self.tabs.add(page, text="身体")
        rows = [("身高（厘米）", self.height, "height_cm", "可超过游戏菜单上限；数据字段上限 327.67"),
                ("臂展（厘米）", self.wingspan, "wingspan_cm", "数据字段上限 327.67；模型变化尚未验证"),
                ("手臂比例（实验）", self.arm_scale, "arm_scale", "数值可保存；目前未观察到模型变化")]
        for i, (label, var, key, hint) in enumerate(rows):
            ttk.Label(page, text=label).grid(row=i, column=0, sticky="w", pady=8)
            entry = ttk.Entry(page, textvariable=var, width=14, justify="center")
            entry.grid(row=i, column=1, sticky="w", padx=14)
            ttk.Label(page, text=hint, style="Muted.TLabel").grid(row=i, column=2, sticky="w")
            self._track(var, entry, [(self.tabs, page)],
                        lambda v=var, k=key: v.get().strip() != str(self.baseline[k]))
        ratio_actions = ttk.Frame(page)
        ratio_actions.grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 10))
        ttk.Button(ratio_actions, text=f"按身高 × {BODY_RATIO} 填臂展",
                   command=lambda: self._fill_body_ratio(from_height=True)).pack(side="left")
        ttk.Button(ratio_actions, text=f"按臂展 ÷ {BODY_RATIO} 填身高",
                   command=lambda: self._fill_body_ratio(from_height=False)).pack(side="left", padx=10)
        check = ttk.Checkbutton(page, text="使用自定义外观比例", variable=self.custom_scales)
        check.grid(row=4, column=0, columnspan=2, sticky="w", pady=10)
        self._track(self.custom_scales, check, [(self.tabs, page)],
                    lambda: self.custom_scales.get() != self.baseline["custom_scales"])
        ttk.Label(page, text=f"{BODY_RATIO} 换算前后均向下取整，身高和臂展都填整数，不自动保存。修改手臂比例会启用自定义外观比例。",
                  style="Muted.TLabel", wraplength=680).grid(row=5, column=0, columnspan=3, sticky="w")

    def _make_rating_tab(self):
        area = ScrollFrame(self.tabs, padding=(20, 14))
        self.tabs.add(area, text="能力")
        inner, columns, row = area.inner, 3, 0
        for group in dict.fromkeys(field["group"] for field in self.fields):
            fields = [field for field in self.fields if field["group"] == group]
            ttk.Label(inner, text=f"{RATING_GROUPS.get(group, group)}  ·  {len(fields)} 项",
                      style="Section.TLabel").grid(row=row, column=0, columnspan=columns * 2, sticky="w",
                                                   pady=(16 if row else 0, 6))
            row += 1
            for n, field in enumerate(fields):
                r, c = row + n // columns, (n % columns) * 2
                ttk.Label(inner, text=field["label"]).grid(row=r, column=c, sticky="w", pady=3, padx=(0, 8))
                var = tk.StringVar()
                self.rating_inputs[field["id"]] = var
                entry = ttk.Entry(inner, textvariable=var, width=6, justify="center")
                entry.grid(row=r, column=c + 1, sticky="w", padx=(0, 30), pady=3)
                self._track(var, entry, [(self.tabs, area)],
                            lambda f=field, v=var: v.get().strip() != str(self.baseline["ratings"][f["id"]]))
            row += (len(fields) + columns - 1) // columns
        ttk.Label(inner, text="能力值范围 25–125；球员总评仍按游戏规则显示，最高为 99。修改后点击下方「保存修改」。",
                  style="Muted.TLabel").grid(
            row=row, column=0, columnspan=columns * 2, sticky="w", pady=(16, 0))
        area.bind_wheel()

    def _bind_shortcuts(self):
        def run(action):
            return lambda _event: (action(), "break")[1]
        for sequence in ("<Control-s>", "<Control-S>"):
            self.bind(sequence, run(self.save))
        for sequence in ("<Control-d>", "<Control-D>"):
            self.bind(sequence, run(self.detect_current))
        for sequence in ("<Control-f>", "<Control-F>"):
            self.bind(sequence, run(lambda: (self.search_entry.focus_set(),
                                             self.search_entry.select_range(0, "end"))))
        self.bind("<F5>", run(self._reload_clicked))

    def _track(self, var: tk.Variable, widget: ttk.Widget, pages: list[tuple], changed):
        """Register an input so edits are highlighted and counted before saving."""
        for notebook, page in pages:
            self._tab_titles.setdefault((notebook, page), notebook.tab(page, "text"))
        self.tracked.append((widget, pages, changed))
        var.trace_add("write", lambda *_: self._schedule_dirty())

    def _schedule_dirty(self):
        if self._dirty_job is None:
            self._dirty_job = self.after_idle(self._refresh_dirty)

    def _refresh_dirty(self) -> int:
        if self._dirty_job is not None:
            try:
                self.after_cancel(self._dirty_job)
            except tk.TclError:
                pass
            self._dirty_job = None
        count, per_tab = 0, Counter()
        for widget, pages, changed in self.tracked:
            try:
                is_changed = bool(self.baseline) and bool(changed())
            except (KeyError, ValueError, TypeError, IndexError):
                is_changed = True
            if is_changed:
                count += 1
                per_tab.update(pages)
            base = widget.winfo_class()
            wanted = f"Changed.{base}" if is_changed else base
            if (str(widget.cget("style")) or base) != wanted:
                widget.configure(style=wanted)
        for (notebook, page), title in self._tab_titles.items():
            text = f"{title} ●" if per_tab[(notebook, page)] else title
            if notebook.tab(page, "text") != text:
                notebook.tab(page, text=text)
        self.dirty_count = count
        self.dirty_text.set(f"● 未保存 {count} 项" if count else "")
        self.title(f"* {APP_TITLE}" if count else APP_TITLE)
        return count

    def _set_connection(self, connected: bool, text: str):
        self.connection_dot.configure(fg=P["success"] if connected else P["danger"])
        self.connection_text.set(text)

    def _schedule_filter(self):
        if self._search_job is not None:
            self.after_cancel(self._search_job)
        self._search_job = self.after(160, self._run_scheduled_filter)

    def _run_scheduled_filter(self):
        self._search_job = None
        self._filter()

    def _select_first_match(self, _event=None):
        if self._search_job is not None:
            self.after_cancel(self._search_job)
            self._run_scheduled_filter()
        first = next(iter(self.player_items), None)
        if first:
            self.player_tree.selection_set(first)
            self.player_tree.see(first)
            self.player_tree.focus(first)
        return "break"

    def _fill_body_ratio(self, *, from_height: bool):
        source_var = self.height if from_height else self.wingspan
        target_var = self.wingspan if from_height else self.height
        source_name = "身高" if from_height else "臂展"
        target_name = "臂展" if from_height else "身高"
        try:
            try:
                source = Decimal(source_var.get().strip())
            except InvalidOperation as exc:
                raise ValueError(f"请先输入有效的{source_name}数值。") from exc
            if not source.is_finite() or not BODY_MIN_CM <= source <= BODY_MAX_CM:
                raise ValueError(f"{source_name}须在 {BODY_MIN_CM} 到 {BODY_MAX_CM} 厘米之间。")
            source = source.to_integral_value(rounding=ROUND_FLOOR)
            target = (source * BODY_RATIO if from_height else source / BODY_RATIO).to_integral_value(
                rounding=ROUND_FLOOR)
            if not BODY_MIN_CM <= target <= BODY_MAX_CM:
                raise ValueError(f"按 {BODY_RATIO} 换算得到的{target_name}为 {target} 厘米，"
                                 f"超出可保存范围 {BODY_MIN_CM}–{BODY_MAX_CM} 厘米。")
            source_var.set(f"{source:.0f}")
            target_var.set(f"{target:.0f}")
            self.status.set(f"已按 {BODY_RATIO} 换算，身高和臂展均向下取整：{source_name} {source:.0f}、{target_name} {target:.0f} 厘米；请点击“保存修改”。")
        except ValueError as exc:
            messagebox.showerror("比例换算失败", str(exc), parent=self)

    def _make_staff_tab(self):
        page = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(page, text="球队员工")
        top = ttk.Frame(page)
        top.pack(fill="x")
        ttk.Label(top, text="球队").pack(side="left")
        self.staff_team_box = ttk.Combobox(top, textvariable=self.staff_team_choice,
                                            state="readonly", width=34)
        self.staff_team_box.pack(side="left", padx=(8, 6), fill="x", expand=True)
        self.staff_team_box.bind("<<ComboboxSelected>>", lambda _event: self._show_staff_team())
        ttk.Button(top, text="重新读取", command=self._refresh_staff_teams).pack(side="left")
        ttk.Button(top, text="批量修改属性", command=self._staff_batch_field).pack(side="left", padx=(6, 0))
        ttk.Button(top, text="批量修改员工", command=self._staff_batch_edit).pack(side="left", padx=(6, 0))
        ttk.Label(page, text="可修改主教练、助理教练、训练师等员工的姓名、职位和主要能力。修改后点击保存员工；可单独撤销。",
                  style="Muted.TLabel").pack(anchor="w", pady=(8, 8))

        body = ttk.PanedWindow(page, orient="horizontal")
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body, style="Card.TFrame", padding=8)
        right_scroll = ScrollFrame(body, padding=14, style="Card.TFrame", background=P["panel"])
        right = right_scroll.inner
        body.add(left, weight=2)
        body.add(right_scroll, weight=3)
        tree_frame = ttk.Frame(left, style="Card.TFrame")
        tree_frame.pack(fill="both", expand=True)
        self.staff_tree = ttk.Treeview(tree_frame, columns=("job", "name", "slot"), show="headings",
                                       selectmode="extended")
        for key, title, width in (("job", "职位", 118), ("name", "姓名", 150), ("slot", "槽位", 48)):
            self.staff_tree.heading(key, text=title, anchor="w")
            self.staff_tree.column(key, width=width, minwidth=42, anchor="w")
        staff_scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.staff_tree.yview)
        self.staff_tree.configure(yscrollcommand=staff_scroll.set)
        self.staff_tree.pack(side="left", fill="both", expand=True)
        staff_scroll.pack(side="right", fill="y")
        self.staff_tree.bind("<<TreeviewSelect>>", self._staff_select)

        ttk.Label(right, text="员工资料", style="CardSection.TLabel").grid(row=0, column=0, columnspan=3,
                                                                            sticky="w", pady=(0, 10))
        ttk.Label(right, text="名").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Entry(right, textvariable=self.staff_first_name, width=22).grid(row=1, column=1, columnspan=2,
                                                                             sticky="w", pady=5)
        ttk.Label(right, text="姓").grid(row=2, column=0, sticky="w", pady=5)
        ttk.Entry(right, textvariable=self.staff_last_name, width=22).grid(row=2, column=1, columnspan=2,
                                                                            sticky="w", pady=5)
        ttk.Label(right, text="职位").grid(row=3, column=0, sticky="w", pady=5)
        self.staff_job_box = ttk.Combobox(right, textvariable=self.staff_job, state="readonly", width=28,
                                          values=self._staff_job_choices())
        self.staff_job_box.grid(row=3, column=1, columnspan=2, sticky="w", pady=5)
        ttk.Separator(right).grid(row=4, column=0, columnspan=3, sticky="ew", pady=(12, 8))
        for row, (key, label, _offset) in enumerate(STAFF_ATTRIBUTE_FIELDS, start=5):
            ttk.Label(right, text=label).grid(row=row, column=0, sticky="w", pady=3)
            var = tk.StringVar()
            self.staff_attribute_inputs[key] = var
            ttk.Entry(right, textvariable=var, width=12, justify="center").grid(row=row, column=1,
                                                                                   sticky="w", pady=3)
            ttk.Label(right, text="0–255", style="Muted.TLabel").grid(row=row, column=2, sticky="w", padx=8)
        buttons = ttk.Frame(right)
        buttons.grid(row=14, column=0, columnspan=3, sticky="w", pady=(14, 0))
        ttk.Button(buttons, text="保存员工", style="Accent.TButton", command=self._save_staff).pack(side="left")
        ttk.Button(buttons, text="撤销员工修改", command=self._undo_staff).pack(side="left", padx=8)
        ttk.Label(right, textvariable=self.staff_status, style="Muted.TLabel", wraplength=380).grid(
            row=15, column=0, columnspan=3, sticky="w", pady=(12, 0))
        ttk.Separator(right).grid(row=16, column=0, columnspan=3, sticky="ew", pady=(16, 8))
        ttk.Label(right, text="高级员工字段（徽章 / 体系熟练度 / 合同 / 其他）",
                  style="CardSection.TLabel").grid(row=17, column=0, columnspan=3, sticky="w", pady=(0, 6))
        SearchBox(right, self.staff_field_search, "搜索字段，例如：徽章、熟练度、三角…", icons=self._icon).grid(
            row=18, column=0, columnspan=3, sticky="ew", pady=(0, 6))
        self.staff_field_search.trace_add("write", lambda *_: self._refresh_staff_fields())
        field_frame = ttk.Frame(right, style="Card.TFrame")
        field_frame.grid(row=19, column=0, columnspan=3, sticky="nsew")
        self.staff_field_tree = ttk.Treeview(field_frame, columns=("category", "field", "value"),
                                             show="headings", height=9)
        for key, title, width in (("category", "类别", 92), ("field", "字段", 220), ("value", "值", 72)):
            self.staff_field_tree.heading(key, text=title, anchor="w")
            self.staff_field_tree.column(key, width=width, minwidth=50, anchor="w")
        field_bar = ttk.Scrollbar(field_frame, orient="vertical", command=self.staff_field_tree.yview)
        self.staff_field_tree.configure(yscrollcommand=field_bar.set)
        self.staff_field_tree.pack(side="left", fill="both", expand=True)
        field_bar.pack(side="right", fill="y")
        self.staff_field_tree.bind("<<TreeviewSelect>>", self._staff_field_select)
        field_controls = ttk.Frame(right, style="Card.TFrame")
        field_controls.grid(row=20, column=0, columnspan=3, sticky="ew", pady=(7, 0))
        ttk.Entry(field_controls, textvariable=self.staff_field_value, width=14, justify="center").pack(side="left")
        ttk.Label(field_controls, text="按字段定义的原始值", style="Muted.TLabel").pack(side="left", padx=8)
        ttk.Button(field_controls, text="保存此字段", command=self._save_staff_field).pack(side="right")
        right.columnconfigure(2, weight=1)
        right.rowconfigure(19, weight=1)
        right_scroll.bind_wheel()

    @staticmethod
    def _staff_job_choices() -> tuple[str, ...]:
        return tuple(f"{value} · {STAFF_JOB_NAMES[value]}" for value in sorted(STAFF_JOB_NAMES))

    @staticmethod
    def _staff_job_value(text: str) -> int:
        return int(text.split("·", 1)[0].strip())

    def _refresh_staff_teams(self):
        if not hasattr(self, "staff_team_box"):
            return
        self.staff_team_map = {}
        if not self.memory or not hasattr(self.memory, "team_options"):
            self.staff_team_box.configure(values=())
            self.staff_team_choice.set("")
            self._clear_staff_form()
            return
        for info in self.memory.team_options():
            label = f"{info['team']} · {info['league']}"
            self.staff_team_map[label] = info
        values = tuple(self.staff_team_map)
        self.staff_team_box.configure(values=values)
        if self.staff_team_choice.get() not in self.staff_team_map:
            self.staff_team_choice.set(values[0] if values else "")
        self._show_staff_team()

    def _show_staff_team(self):
        self.staff_tree.delete(*self.staff_tree.get_children())
        self.staff_items = {}
        self.staff_selected = None
        team = self.staff_team_map.get(self.staff_team_choice.get())
        if not team or not self.memory:
            self._clear_staff_form()
            self.staff_status.set("选择球队后读取员工")
            return
        try:
            staff_list = self.memory.team_staff(team)
            for item in sorted(staff_list, key=lambda entry: (entry["job"], entry["slot"])):
                iid = f"staff:{item['slot']}"
                self.staff_items[iid] = item
                job = STAFF_JOB_NAMES.get(item["job"], f"未知职位 {item['job']}")
                self.staff_tree.insert("", "end", iid=iid,
                                       values=(job, item["name"], item["slot"] + 1))
            if staff_list:
                first = next(iter(self.staff_tree.get_children()), None)
                if first:
                    self.staff_tree.selection_set(first)
                    self.staff_tree.focus(first)
                    self.staff_tree.see(first)
            self.staff_status.set(f"{team['team']}：读取 {len(staff_list)} 名员工")
            self._refresh_staff_fields()
        except Exception as exc:
            self._clear_staff_form()
            self.staff_status.set(str(exc))

    def _staff_select(self, _event=None):
        selected = self.staff_tree.selection()
        item = self.staff_items.get(selected[0]) if len(selected) == 1 else None
        self.staff_selected = item
        if not item:
            self._clear_staff_form()
            self.staff_status.set(f"已选择 {len(selected)} 名员工；可使用批量修改") if selected else None
            self._refresh_staff_fields()
            return
        self.staff_first_name.set(item["first_name"])
        self.staff_last_name.set(item["last_name"])
        self.staff_job.set(f"{item['job']} · {STAFF_JOB_NAMES.get(item['job'], f'未知职位 {item['job']}')}")
        for key, var in self.staff_attribute_inputs.items():
            var.set(str(item["attributes"].get(key, 0)))
        self._refresh_staff_fields()

    def _staff_selected_items(self) -> list[dict]:
        return [self.staff_items[iid] for iid in self.staff_tree.selection() if iid in self.staff_items]

    def _staff_field_raw(self, staff: dict, field: dict):
        raw = self.memory.read(staff["address"] + field["offset"], 4)
        if field["kind"] == "float":
            return round(struct.unpack_from("<f", raw)[0], 5)
        word = struct.unpack_from("<I", raw)[0]
        return (word >> field["shift"]) & ((1 << field["bits"]) - 1)

    @staticmethod
    def _staff_field_pack(old_word: int, field: dict, value: int | float) -> bytes:
        if field["kind"] == "float":
            return struct.pack("<f", float(value))
        maximum = (1 << field["bits"]) - 1
        if not 0 <= int(value) <= maximum:
            raise ValueError(f"{field['label']} 须在 0 到 {maximum} 之间")
        mask = maximum << field["shift"]
        return struct.pack("<I", (old_word & ~mask) | (int(value) << field["shift"]))

    def _refresh_staff_fields(self):
        if not hasattr(self, "staff_field_tree"):
            return
        self.staff_field_tree.delete(*self.staff_field_tree.get_children())
        self.staff_field_items = {}
        staff = self.staff_selected
        if not self.memory or not staff:
            self.staff_field_value.set("")
            self.staff_field_selected = None
            return
        term = self.staff_field_search.get().strip().casefold()
        for index, field in enumerate(self.staff_fields):
            haystack = f"{field['section']} {field['group']} {field['id']} {field['label']}".casefold()
            if term and term not in haystack:
                continue
            iid = f"field:{index}"
            self.staff_field_items[iid] = field
            self.staff_field_tree.insert("", "end", iid=iid,
                                         values=(field["section"], field["label"], self._staff_field_raw(staff, field)))
        self.staff_field_selected = None

    def _staff_field_select(self, _event=None):
        selected = self.staff_field_tree.selection()
        field = self.staff_field_items.get(selected[0]) if selected else None
        self.staff_field_selected = field
        if field and self.staff_selected:
            self.staff_field_value.set(str(self._staff_field_raw(self.staff_selected, field)))
        else:
            self.staff_field_value.set("")

    def _staff_changes_for_field(self, staff: dict, field: dict, value: str) -> dict[int, bytes]:
        try:
            parsed = float(value.strip()) if field["kind"] == "float" else int(value.strip(), 0)
        except ValueError:
            raise ValueError(f"{field['label']}：请输入有效数值") from None
        if field["kind"] == "float" and not -1e9 <= parsed <= 1e9:
            raise ValueError(f"{field['label']} 超出允许范围")
        old_word = self.memory.u32(staff["address"] + field["offset"])
        packed = self._staff_field_pack(old_word, field, parsed)
        if packed == self.memory.read(staff["address"] + field["offset"], 4):
            return {}
        return {field["offset"]: packed}

    def _save_staff_field(self):
        if not self.staff_selected or not self.staff_field_selected:
            self.staff_status.set("请先选择一名员工和一个高级字段")
            return
        try:
            changes = self._staff_changes_for_field(self.staff_selected, self.staff_field_selected,
                                                    self.staff_field_value.get())
            if not changes:
                self.staff_status.set("该字段没有变化")
                return
            self.staff_last_backup = self.memory.apply_staff(self.staff_selected, changes,
                                                              label=self.staff_selected["name"])
            self._show_staff_team()
            self.staff_status.set("已保存员工高级字段；可撤销")
        except Exception as exc:
            messagebox.showerror("保存员工字段失败", str(exc), parent=self)

    def _staff_batch_field(self):
        staff_list = self._staff_selected_items()
        field = self.staff_field_selected
        if not staff_list or not field:
            messagebox.showinfo("批量修改属性", "请在左侧多选员工，并在高级字段列表中选择一个字段。", parent=self)
            return
        dialog = tk.Toplevel(self)
        dialog.title("批量修改属性")
        dialog.transient(self)
        dialog.grab_set()
        dialog.geometry("460x190")
        ttk.Label(dialog, text=f"将「{field['label']}」应用到 {len(staff_list)} 名员工。", wraplength=410).pack(
            anchor="w", padx=18, pady=(18, 10))
        value = tk.StringVar(value=self.staff_field_value.get())
        row = ttk.Frame(dialog)
        row.pack(fill="x", padx=18)
        ttk.Label(row, text="统一值").pack(side="left")
        ttk.Entry(row, textvariable=value, width=18).pack(side="left", padx=10)
        ttk.Label(row, text="原始值").pack(side="left")
        buttons = ttk.Frame(dialog)
        buttons.pack(fill="x", padx=18, pady=20)
        def apply():
            try:
                updates = [(staff, self._staff_changes_for_field(staff, field, value.get()))
                           for staff in staff_list]
                updates = [(staff, changes) for staff, changes in updates if changes]
                if not updates:
                    raise ValueError("所有选中员工已经是这个值")
                self.staff_last_backup = self.memory.apply_staff_many(updates, label=field["label"])
                dialog.destroy()
                self._show_staff_team()
                self.staff_status.set(f"已批量修改 {len(updates)} 名员工的{field['label']}；可撤销")
            except Exception as exc:
                messagebox.showerror("批量修改属性失败", str(exc), parent=dialog)
        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side="right")
        ttk.Button(buttons, text="应用到选中员工", style="Accent.TButton", command=apply).pack(side="right", padx=8)
        dialog.bind("<Escape>", lambda _e: dialog.destroy())

    def _staff_batch_edit(self):
        staff_list = self._staff_selected_items()
        if not staff_list:
            messagebox.showinfo("批量修改员工", "请先在左侧多选员工。", parent=self)
            return
        dialog = tk.Toplevel(self)
        dialog.title("批量修改员工")
        dialog.transient(self)
        dialog.grab_set()
        dialog.geometry("520x520")
        ttk.Label(dialog, text=f"对 {len(staff_list)} 名员工应用；空白属性保持原值。", style="Muted.TLabel").pack(
            anchor="w", padx=18, pady=(16, 10))
        form = ttk.Frame(dialog)
        form.pack(fill="both", expand=True, padx=18)
        job = tk.StringVar(value="不修改")
        ttk.Label(form, text="职位").grid(row=0, column=0, sticky="w", pady=5)
        ttk.Combobox(form, textvariable=job, state="readonly", width=28,
                     values=("不修改", *self._staff_job_choices())).grid(row=0, column=1, sticky="w", pady=5)
        values: dict[str, tk.StringVar] = {}
        for row, (key, label, _offset) in enumerate(STAFF_ATTRIBUTE_FIELDS, start=1):
            values[key] = tk.StringVar()
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=4)
            ttk.Entry(form, textvariable=values[key], width=14).grid(row=row, column=1, sticky="w", pady=4)
            ttk.Label(form, text="空白保持原值").grid(row=row, column=2, sticky="w", padx=8)
        buttons = ttk.Frame(dialog)
        buttons.pack(fill="x", padx=18, pady=16)
        def apply():
            try:
                updates=[]
                selected_job = None if job.get()=="不修改" else self._staff_job_value(job.get())
                for staff in staff_list:
                    changes={}
                    if selected_job is not None:
                        old=self.memory.u32(staff['address']+STAFF_JOB_OFFSET)
                        mask=((1<<STAFF_JOB_BITS)-1)<<STAFF_JOB_SHIFT
                        changes[STAFF_JOB_OFFSET]=struct.pack('<I',(old&~mask)|(selected_job<<STAFF_JOB_SHIFT))
                        target=self.memory.u32(staff['address']+STAFF_TARGET_JOB_OFFSET)
                        tmask=((1<<STAFF_JOB_BITS)-1)<<STAFF_TARGET_JOB_SHIFT
                        changes[STAFF_TARGET_JOB_OFFSET]=struct.pack('<I',(target&~tmask)|(selected_job<<STAFF_TARGET_JOB_SHIFT))
                    for key,label,offset in STAFF_ATTRIBUTE_FIELDS:
                        text=values[key].get().strip()
                        if not text: continue
                        number=int(text,0)
                        if not 0<=number<=255: raise ValueError(f'{label} 须在 0 到 255 之间')
                        changes[offset]=bytes([number])
                    if changes: updates.append((staff,changes))
                if not updates: raise ValueError('没有填写要修改的内容')
                self.staff_last_backup=self.memory.apply_staff_many(updates,label='职位和基础属性')
                dialog.destroy(); self._show_staff_team()
                self.staff_status.set(f'已批量修改 {len(updates)} 名员工；可撤销')
            except (ValueError, RuntimeError, OSError) as exc:
                messagebox.showerror('批量修改员工失败',str(exc),parent=dialog)
        ttk.Button(buttons,text='取消',command=dialog.destroy).pack(side='right')
        ttk.Button(buttons,text='应用到选中员工',style='Accent.TButton',command=apply).pack(side='right',padx=8)
        dialog.bind('<Escape>',lambda _e:dialog.destroy())

    def _clear_staff_form(self):
        self.staff_selected = None
        self.staff_first_name.set("")
        self.staff_last_name.set("")
        self.staff_job.set("")
        for var in self.staff_attribute_inputs.values():
            var.set("")

    def _save_staff(self):
        staff = self.staff_selected
        if not self.memory or not staff:
            self.staff_status.set("请先选择一名员工")
            return
        try:
            first = self.staff_first_name.get().strip()
            last = self.staff_last_name.get().strip()
            if not first or not last:
                raise ValueError("员工姓名不能为空")
            job = self._staff_job_value(self.staff_job.get())
            changes: dict[int, bytes] = {}
            if first != staff["first_name"]:
                changes[STAFF_FIRST_OFFSET] = encode_name(first)
            if last != staff["last_name"]:
                changes[STAFF_LAST_OFFSET] = encode_name(last)
            old_word = self.memory.u32(staff["address"] + STAFF_JOB_OFFSET)
            job_mask = ((1 << STAFF_JOB_BITS) - 1) << STAFF_JOB_SHIFT
            new_word = (old_word & ~job_mask) | (job << STAFF_JOB_SHIFT)
            if job != staff["job"]:
                changes[STAFF_JOB_OFFSET] = struct.pack("<I", new_word)
            target_word = self.memory.u32(staff["address"] + STAFF_TARGET_JOB_OFFSET)
            target_mask = ((1 << STAFF_JOB_BITS) - 1) << STAFF_TARGET_JOB_SHIFT
            new_target_word = (target_word & ~target_mask) | (job << STAFF_TARGET_JOB_SHIFT)
            if job != staff.get("target_job"):
                changes[STAFF_TARGET_JOB_OFFSET] = struct.pack("<I", new_target_word)
            for key, _label, offset in STAFF_ATTRIBUTE_FIELDS:
                try:
                    value = int(self.staff_attribute_inputs[key].get().strip())
                except ValueError:
                    raise ValueError(f"{dict((k, label) for k, label, _ in STAFF_ATTRIBUTE_FIELDS)[key]}：请输入整数") from None
                if not 0 <= value <= 255:
                    raise ValueError(f"{key} 须在 0 到 255 之间")
                if value != staff["attributes"].get(key):
                    changes[offset] = bytes([value])
            if not changes:
                self.staff_status.set("该员工没有需要保存的变化")
                return
            backup = self.memory.apply_staff(staff, changes, label=staff["name"])
            self.staff_last_backup = backup
            self._show_staff_team()
            self.staff_status.set(f"已保存员工修改（{len(changes)} 处）；可撤销。")
        except Exception as exc:
            messagebox.showerror("保存员工失败", str(exc), parent=self)

    def _undo_staff(self):
        if not self.memory or not self.staff_last_backup:
            self.staff_status.set("没有可撤销的员工修改")
            return
        if not messagebox.askyesno("撤销员工修改", "恢复上次保存的员工数据？", parent=self):
            return
        try:
            self.memory.undo(self.staff_last_backup)
            self.staff_last_backup = None
            self._show_staff_team()
            self.staff_status.set("已撤销上次员工修改")
        except Exception as exc:
            messagebox.showerror("撤销员工失败", str(exc), parent=self)

    def _make_profile_tab(self):
        area = ScrollFrame(self.tabs, padding=(20, 14))
        self.tabs.add(area, text="个人资料")
        inner = area.inner
        ttk.Label(inner, text="修改姓名后，游戏可能需要重新打开球员页面才能刷新显示。",
                  style="Muted.TLabel").grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 12))
        for n, (key, label) in enumerate(PROFILE_UI):
            row, column = 1 + n // 2, (n % 2) * 2
            ttk.Label(inner, text=label).grid(row=row, column=column, sticky="w", pady=5, padx=(0, 12))
            var = tk.StringVar()
            self.profile_inputs[key] = var
            options = POSITIONS if key in ("position", "secondary_position") else (
                HANDS if key == "dominant_hand" else DUNK_HANDS if key == "dunk_hand" else None)
            if options:
                widget = ttk.Combobox(inner, textvariable=var, values=options, state="readonly", width=20)
            else:
                widget = ttk.Entry(inner, textvariable=var, width=22)
            widget.grid(row=row, column=column + 1, sticky="w", padx=(0, 40), pady=5)
            self._track(var, widget, [(self.tabs, area)],
                        lambda k=key, v=var: v.get().strip() != self._profile_display(k, self.baseline["profile"][k]))
        area.bind_wheel()

    def _make_signature_tab(self):
        outer = ttk.Frame(self.tabs, padding=(4, 10, 4, 4))
        self.tabs.add(outer, text="动作")
        bar = ttk.Frame(outer)
        bar.pack(fill="x", padx=10, pady=(0, 4))
        ttk.Label(bar, text="动作预设").pack(side="left")
        self.signature_preset_box = ttk.Combobox(bar, textvariable=self.signature_preset_name, width=26)
        self.signature_preset_box.pack(side="left", padx=(8, 12))
        ttk.Label(bar, text="范围").pack(side="left")
        ttk.Combobox(bar, textvariable=self.signature_preset_scope, state="readonly", width=9,
                     values=(ALL_SIGNATURES, *SIGNATURE_GROUPS.values())).pack(side="left", padx=(8, 12))
        ttk.Button(bar, text="载入预设", command=self._load_signature_preset).pack(side="left")
        ttk.Button(bar, text="保存为预设", command=self._save_signature_preset).pack(side="left", padx=6)
        ttk.Button(bar, text="删除", command=lambda: self._delete_preset("signatures")).pack(side="left")
        ttk.Label(outer, textvariable=self.signature_preset_info, style="Muted.TLabel").pack(
            anchor="w", padx=10, pady=(0, 2))
        self.signature_preset_name.trace_add("write", lambda *_: self._show_signature_preset_info())
        self.signature_preset_scope.trace_add("write", lambda *_: self._show_signature_preset_info())
        self._show_signature_preset_info()
        ttk.Label(outer, text="从列表选动作名称，也可直接输入编号；未收录名称的项目保留数字输入。修改后点「保存修改」。",
                  style="Muted.TLabel").pack(anchor="w", padx=10, pady=(0, 6))
        book = ttk.Notebook(outer, style="Sub.TNotebook")
        book.pack(fill="both", expand=True)
        for group, title in SIGNATURE_GROUPS.items():
            area = ScrollFrame(book, padding=(16, 12))
            book.add(area, text=title)
            fields = [field for field in self.signature_fields if field["group"] == group]
            for row, field in enumerate(fields):
                ttk.Label(area.inner, text=signature_label(field)).grid(row=row, column=0, sticky="w", pady=4,
                                                                        padx=(0, 12))
                var = tk.StringVar()
                self.signature_inputs[field["id"]] = var
                options = self.signature_options.get(field["id"])
                if options:
                    labels = [f"{index} · {signature_option_name(name)}" for index, name in enumerate(options)]
                    widget = ttk.Combobox(area.inner, textvariable=var, values=labels, width=40)
                else:
                    widget = ttk.Entry(area.inner, textvariable=var, width=16)
                widget.grid(row=row, column=1, sticky="w", pady=4)
                hint = f"{len(options)} 个已收录名称" if options else f"编号 0–{(1 << field['bits']) - 1}"
                ttk.Label(area.inner, text=hint, style="Muted.TLabel").grid(row=row, column=2, sticky="w", padx=12)
                self._track(var, widget, [(self.tabs, outer), (book, area)],
                            lambda f=field, v=var: self._signature_value(f, v.get()) != self.baseline["signatures"][f["id"]])
            area.bind_wheel()

    def _make_playbook_tab(self):
        page = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(page, text="战术手册")
        self.playbook_page = page
        top = ttk.Frame(page)
        top.pack(fill="x")
        ttk.Label(top, text="手册").pack(side="left")
        ttk.Entry(top, textvariable=self.book_search, width=22).pack(side="left", padx=(8, 5))
        self.book_search.trace_add("write", lambda *_: self._filter_book_options())
        self.book_box = ttk.Combobox(top, textvariable=self.book_choice, state="readonly", width=37)
        self.book_box.pack(side="left", fill="x", expand=True)
        self.book_box.bind("<<ComboboxSelected>>", lambda _event: self._show_playbook())
        ttk.Button(top, text="重新读取", command=self._refresh_playbooks).pack(side="left", padx=6)

        presets = ttk.Frame(page)
        presets.pack(fill="x", pady=(8, 0))
        ttk.Label(presets, text="手册预设").pack(side="left")
        self.playbook_preset_box = ttk.Combobox(presets, textvariable=self.playbook_preset_name, width=28)
        self.playbook_preset_box.pack(side="left", padx=(8, 12))
        self.playbook_preset_box.bind("<<ComboboxSelected>>", lambda _event: self._show_playbook_preset_info())
        ttk.Button(presets, text="保存整本", command=lambda: self._save_playbook_preset(selected_only=False)).pack(
            side="left")
        self.playbook_save_selected = ttk.Button(presets, text="保存所选",
                                                 command=lambda: self._save_playbook_preset(selected_only=True))
        self.playbook_save_selected.pack(side="left", padx=6)
        self.playbook_save_selected.state(["disabled"])
        ttk.Button(presets, text="替换当前手册", command=lambda: self._apply_playbook_preset("replace")).pack(
            side="left", padx=(12, 0))
        ttk.Button(presets, text="追加到空槽", command=lambda: self._apply_playbook_preset("append")).pack(
            side="left", padx=6)
        ttk.Button(presets, text="删除", command=lambda: self._delete_preset("playbooks")).pack(side="left", padx=(6, 0))

        note = ("左侧选择球队手册的槽位，右侧从已载入战术中挑选；按住 Ctrl/Shift 可多选并批量添加或删除。\n"
                "打法、细分和位置读取自游戏战术数据，位置先列主攻球员；「空接」按战术名中的 ALLEY / LOB 标出。")
        ttk.Label(page, text=note, foreground=P["muted"], wraplength=1050).pack(anchor="w", pady=(7, 4))
        panes = ttk.PanedWindow(page, orient="horizontal")
        panes.pack(fill="both", expand=True)
        left = ttk.Frame(panes)
        right = ttk.Frame(panes)
        panes.add(left, weight=2)
        panes.add(right, weight=3)

        ttk.Label(left, text="当前手册（前 80 槽可修改；81–88 为保留槽位）").pack(anchor="w")
        slot_frame = ttk.Frame(left)
        slot_frame.pack(fill="both", expand=True, pady=(4, 5))
        self.play_slot_tree = ttk.Treeview(slot_frame, columns=("type", "position"),
                                           show="tree headings", selectmode="extended")
        self.play_slot_tree.heading("#0", text="槽位 / 战术名称")
        self.play_slot_tree.heading("type", text="细分")
        self.play_slot_tree.heading("position", text="关联位置")
        self.play_slot_tree.column("#0", width=265, stretch=True)
        self.play_slot_tree.column("type", width=100, stretch=False)
        self.play_slot_tree.column("position", width=125, stretch=False)
        self.play_slot_tree.bind("<<TreeviewSelect>>", lambda _event: self._update_playbook_preset_buttons())
        slot_scroll = ttk.Scrollbar(slot_frame, orient="vertical", command=self.play_slot_tree.yview)
        self.play_slot_tree.configure(yscrollcommand=slot_scroll.set)
        self.play_slot_tree.pack(side="left", fill="both", expand=True)
        slot_scroll.pack(side="right", fill="y")
        slot_actions = ttk.Frame(left)
        slot_actions.pack(side="bottom", fill="x", before=slot_frame)
        ttk.Button(slot_actions, text="替换所选槽位", command=lambda: self._change_playbook("replace")).pack(
            side="left", padx=(0, 4))
        ttk.Button(slot_actions, text="加入空槽", command=lambda: self._change_playbook("add")).pack(
            side="left", padx=4)
        ttk.Button(slot_actions, text="清空槽位", command=lambda: self._change_playbook("clear")).pack(
            side="left", padx=4)
        ttk.Button(slot_actions, text="批量删除所选", command=self._batch_clear_playbook).pack(
            side="left", padx=4)

        filters = ttk.Frame(right)
        filters.pack(fill="x")
        self.play_type_box = ttk.Combobox(filters, textvariable=self.play_type_filter, state="readonly",
                                           values=("全部打法", *TYPE_ORDER), width=9)
        self.play_type_box.pack(side="left", padx=(0, 4))
        self.play_detail_box = ttk.Combobox(filters, textvariable=self.play_detail_filter, state="readonly",
                                             values=("全部细分", *DETAIL_ORDER), width=9)
        self.play_detail_box.pack(side="left", padx=4)
        self.play_position_box = ttk.Combobox(filters, textvariable=self.play_position_filter,
                                               state="readonly", values=("全部位置", *POSITION_NAMES, "未标注"), width=9)
        self.play_position_box.pack(side="left", padx=4)
        ttk.Entry(filters, textvariable=self.play_search, width=18).pack(side="left", fill="x", expand=True, padx=4)
        self.play_type_box.bind("<<ComboboxSelected>>", lambda _event: self._play_type_changed())
        self.play_detail_box.bind("<<ComboboxSelected>>", lambda _event: self._filter_play_catalog())
        self.play_position_box.bind("<<ComboboxSelected>>", lambda _event: self._filter_play_catalog())
        self.play_search.trace_add("write", lambda *_: self._filter_play_catalog())
        ttk.Label(right, text="可选战术（打法 → 细分，如三分、空接；可搜索英文名、细分或 CRC）").pack(anchor="w", pady=(7, 4))
        catalog_frame = ttk.Frame(right)
        catalog_frame.pack(fill="both", expand=True)
        self.play_catalog_tree = ttk.Treeview(catalog_frame, columns=("detail", "position", "books"),
                                              show="tree headings", selectmode="extended")
        self.play_catalog_tree.heading("#0", text="打法 / 细分 / 战术名称")
        self.play_catalog_tree.heading("detail", text="细分")
        self.play_catalog_tree.heading("position", text="关联位置")
        self.play_catalog_tree.heading("books", text="使用次数")
        self.play_catalog_tree.column("#0", width=290, stretch=True)
        self.play_catalog_tree.column("detail", width=90, stretch=False)
        self.play_catalog_tree.column("position", width=125, stretch=False)
        self.play_catalog_tree.column("books", width=65, stretch=False, anchor="center")
        catalog_scroll = ttk.Scrollbar(catalog_frame, orient="vertical", command=self.play_catalog_tree.yview)
        self.play_catalog_tree.configure(yscrollcommand=catalog_scroll.set)
        self.play_catalog_tree.pack(side="left", fill="both", expand=True)
        catalog_scroll.pack(side="right", fill="y")
        # Bottom rows are packed ahead of the lists so a short window shrinks the lists, not the buttons.
        ttk.Button(right, text="批量添加所选战术到空槽", style="Accent.TButton", command=self._batch_add_playbook).pack(
            side="bottom", anchor="e", pady=(5, 0), before=catalog_frame)
        footer = ttk.Frame(page)
        footer.pack(side="bottom", fill="x", pady=(6, 0), before=panes)
        ttk.Label(footer, textvariable=self.playbook_status).pack(side="left")
        ttk.Button(footer, text="撤销上次战术修改", command=self._undo_playbook).pack(side="right")

    def _signature_display(self, field: dict, value: int) -> str:
        options = self.signature_options.get(field["id"])
        if not options:
            return str(value)
        name = signature_option_name(options[value]) if 0 <= value < len(options) else "未收录名称"
        return f"{value} · {name}"

    def _signature_value(self, field: dict, text: str) -> int:
        text = text.strip()
        number = re.match(r"^(\d+)(?:\s*[·.:\-].*)?$", text)
        if number:
            return int(number.group(1))
        options = self.signature_options.get(field["id"], [])
        needle = text.casefold().replace("_", " ")
        matches = [index for index, name in enumerate(options)
                   if signature_option_name(name).casefold() == needle]
        if len(matches) == 1:
            return matches[0]
        matches = [index for index, name in enumerate(options)
                   if signature_option_name(name).casefold().startswith(needle)]
        if len(matches) == 1:
            return matches[0]
        raise ValueError(f"{signature_label(field)}：请输入动作编号或从列表选择名称")

    def batch_edit(self):
        if not self.memory or not self.selected or not self.baseline:
            messagebox.showinfo("批量修改", "请先选择或识别一名球员。", parent=self)
            return
        if self._has_pending_edits():
            messagebox.showinfo("批量修改", "请先保存或重新读取当前手动修改，再进行批量修改。", parent=self)
            return
        rating_groups = {"进攻": "Offense", "防守": "Defense", "运动": "Athleticism",
                         "耐久": "Durability", "意识": "Mental", "其他": "Misc"}
        extra_groups = {"Jump Shooting": "跳投", "Layups And Dunks": "上篮与扣篮", "Drive Setup": "突破准备",
                        "Driving": "突破", "Passing": "传球", "Post Game": "背身", "Freelance": "自由进攻",
                        "Defense": "防守", "Hot Zones": "热区", "Inside Scoring": "篮下得分",
                        "Outside Scoring": "外线得分", "Playmaking": "组织", "Defending": "防守",
                        "Athleticism": "运动", "Rebounding": "篮板", "Personality": "个性",
                        "Flags": "标记", "Gameplay": "比赛"}
        scopes: dict[str, tuple[str, list[dict], int, int]] = {}
        scopes["能力 · 全部"] = ("ratings", self.fields, RATING_MIN, RATING_MAX)
        for title, group in rating_groups.items():
            scopes[f"能力 · {title}"] = ("ratings", [f for f in self.fields if f["group"] == group], RATING_MIN, RATING_MAX)
        categories = (("Tendencies", 7, "倾向", 0, 100), ("Tendencies", 2, "热区", 0, 3),
                      ("Badges", 3, "徽章等级", 0, 5), ("Badges", 1, "徽章开关", 0, 1))
        for section, bits, title, low, high in categories:
            fields = [f for f in self.extra_fields if f["section"] == section and f["bits"] == bits]
            scopes[f"{title} · 全部"] = ("extras", fields, low, high)
            for group in dict.fromkeys(f["group"] for f in fields):
                scopes[f"{title} · {extra_groups.get(group, group)}"] = (
                    "extras", [f for f in fields if f["group"] == group], low, high)
        for section, title in (("Tendencies", "倾向与热区"), ("Badges", "全部徽章")):
            fields = [f for f in self.extra_fields if f["section"] == section]
            scopes[f"{title} · 设到最高"] = ("preset_high", fields, 0, 0)
            scopes[f"{title} · 全部清零"] = ("preset_zero", fields, 0, 0)
        dialog = tk.Toplevel(self)
        dialog.title("批量修改")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.bind("<Map>", lambda event: dialog.after_idle(lambda: theme.dark_title_bar(dialog))
                    if event.widget is dialog else None, add="+")
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=f"当前球员：{self.selected['name']}").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 14))
        ttk.Label(frame, text="修改范围").grid(row=1, column=0, sticky="w", pady=6)
        scope = tk.StringVar(value="能力 · 全部")
        ttk.Combobox(frame, textvariable=scope, values=list(scopes), state="readonly", width=26).grid(row=1, column=1, sticky="w")
        value_hint = tk.StringVar(value=f"目标数值（{RATING_MIN}–{RATING_MAX}）")
        ttk.Label(frame, textvariable=value_hint).grid(row=2, column=0, sticky="w", pady=6)
        value = tk.StringVar(value="99")
        entry = ttk.Entry(frame, textvariable=value, width=20)
        entry.grid(row=2, column=1, sticky="w")
        ttk.Label(frame, text="会直接保存到游戏；可用主界面的“撤销上次保存”恢复。", foreground=P["muted"]).grid(row=3, column=0, columnspan=2, sticky="w", pady=(10, 16))

        def on_scope_changed(*_):
            kind, _, low, high = scopes[scope.get()]
            if kind.startswith("preset"):
                value_hint.set("目标数值：各项自动设定")
                value.set("无需填写")
                entry.configure(state="disabled")
            else:
                entry.configure(state="normal")
                value_hint.set(f"目标数值（{low}–{high}）")
                value.set(str(high))
        scope.trace_add("write", on_scope_changed)

        def apply_batch():
            try:
                kind, fields, low, high = scopes[scope.get()]
                target = None if kind.startswith("preset") else int(value.get().strip())
                if target is not None and not low <= target <= high:
                    raise ValueError(f"目标数值须在 {low} 到 {high} 之间")
                masks = {}
                if kind == "ratings":
                    changes = {field["offset"]: bytes([rating_to_raw(target)]) for field in fields
                               if self.baseline["ratings"][field["id"]] != target}
                    changed = len(changes)
                else:
                    updates = []
                    for field in fields:
                        if kind == "preset_zero":
                            wanted = 0
                        elif kind == "preset_high":
                            wanted = (100 if field["section"] == "Tendencies" and field["bits"] == 7 else
                                      3 if field["section"] == "Tendencies" else
                                      5 if field["bits"] == 3 else 1)
                        else:
                            wanted = target
                        if self.baseline["extras"][field["section"] + ":" + field["id"]] != wanted:
                            updates.append((field, wanted))
                    changes, masks = self._pack_extra_updates(updates)
                    changed = len(updates)
                if not changes:
                    self.status.set("所选项目已经是目标数值")
                    dialog.destroy()
                    return
                backup = self.memory.apply(self.selected, changes, {}, edit_record=self.current_edit_address, masks=masks)
                self.last_backup = backup
                self._refresh_selected_record()
                self.status.set(f"已批量修改 {changed} 项；备份：{backup.name}")
                dialog.destroy()
            except Exception as exc:
                messagebox.showerror("批量修改失败", str(exc), parent=dialog)

        ttk.Button(frame, text="取消", command=dialog.destroy).grid(row=4, column=0, sticky="e", padx=8)
        ttk.Button(frame, text="保存修改", style="Accent.TButton", command=apply_batch).grid(row=4, column=1, sticky="e")
        dialog.bind("<Escape>", lambda _e: dialog.destroy())
        dialog.bind("<Return>", lambda _e: apply_batch())
        entry.focus_set()

    def copy_dna_dialog(self):
        if not self.memory or not self.selected or not self.baseline:
            messagebox.showinfo("复制 DNA", "请先选择或识别一名来源球员。", parent=self)
            return
        if self._has_pending_edits():
            messagebox.showinfo("复制 DNA", "请先保存当前修改，再复制 DNA。", parent=self)
            return
        source = self.selected
        dialog = tk.Toplevel(self)
        dialog.title("复制球员 DNA")
        self.update_idletasks()
        dialog_x = max(0, self.winfo_rootx() + (self.winfo_width() - 720) // 2)
        dialog_y = max(0, self.winfo_rooty() + (self.winfo_height() - 700) // 2)
        dialog.geometry(f"720x700+{dialog_x}+{dialog_y}")
        dialog.minsize(680, 660)
        dialog.transient(self)
        dialog.bind("<Map>", lambda event: dialog.after_idle(lambda: theme.dark_title_bar(dialog))
                    if event.widget is dialog else None, add="+")
        frame = ttk.Frame(dialog, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=f"来源：{source['name']} · {source['team']} · #{source['index']}",
                  style="Title.TLabel").pack(anchor="w", pady=(0, 10))
        ttk.Label(frame, text="数据 DNA：能力、倾向、徽章、动作；外貌 DNA：身材、面部参数、头发与穿戴。",
                  foreground=P["muted"]).pack(anchor="w")
        kind = tk.StringVar(value="data")
        types = ttk.Frame(frame)
        types.pack(fill="x", pady=10)
        ttk.Radiobutton(types, text="数据 DNA", variable=kind, value="data").pack(side="left")
        ttk.Radiobutton(types, text="外貌 DNA", variable=kind, value="appearance").pack(side="left", padx=20)
        scope = tk.StringVar(value="single")
        scopes = ttk.Frame(frame)
        scopes.pack(fill="x", pady=(0, 8))
        ttk.Radiobutton(scopes, text="复制给一名球员", variable=scope, value="single").pack(side="left")
        ttk.Radiobutton(scopes, text="复制给整支球队", variable=scope, value="team").pack(side="left", padx=20)
        ttk.Label(frame, text="目标联赛").pack(anchor="w")
        league = tk.StringVar(value="全部联赛")
        league_box = ttk.Combobox(frame, textvariable=league, state="readonly",
                                  values=("全部联赛", *LEAGUE_ORDER))
        league_box.pack(fill="x", pady=(4, 8))
        ttk.Label(frame, text="目标球队").pack(anchor="w")
        team = tk.StringVar(value="全部球队")
        team_box = ttk.Combobox(frame, textvariable=team, state="readonly",
                                 values=("全部球队", *sorted({p["team"] for p in self.memory.players})))
        team_box.pack(fill="x", pady=(4, 8))
        ttk.Label(frame, text="查找目标球员（单人复制时选择）").pack(anchor="w")
        query = tk.StringVar()
        ttk.Entry(frame, textvariable=query).pack(fill="x", pady=(4, 8))
        footer = ttk.Frame(frame)
        footer.pack(side="bottom", fill="x")
        target_frame = ttk.Frame(frame)
        target_frame.pack(fill="both", expand=True)
        target_list = ttk.Treeview(target_frame, columns=("team", "rating"),
                                  selectmode="browse", show="tree headings", height=7)
        target_list.heading("#0", text="目标球员")
        target_list.heading("team", text="球队")
        target_list.heading("rating", text="总评")
        target_list.column("#0", width=235, minwidth=160)
        target_list.column("team", width=230, minwidth=120)
        target_list.column("rating", width=60, minwidth=50, stretch=False, anchor="center")
        target_scroll = ttk.Scrollbar(target_frame, orient="vertical", command=target_list.yview)
        target_list.configure(yscrollcommand=target_scroll.set)
        target_scroll.pack(side="right", fill="y")
        target_list.pack(side="left", fill="both", expand=True)
        candidates = {}

        def refresh(*_args):
            candidates.clear()
            target_list.delete(*target_list.get_children())
            term = query.get().strip().casefold()
            selected_team = team.get()
            for player in self.memory.players:
                if player["index"] == source["index"]:
                    continue
                if league.get() != "全部联赛" and player["league"] != league.get():
                    continue
                if selected_team != "全部球队" and player["team"] != selected_team:
                    continue
                if term and term not in player["name"].casefold() and term != str(player["index"]):
                    continue
                item_id = str(player["index"])
                candidates[item_id] = player
                info = self.memory.team_info(player)
                rating = player["overall"] if player["overall"] is not None else "—"
                target_list.insert("", "end", iid=item_id,
                                   text=f"  {player['name']} · #{player['index']}",
                                   image=self.badges.pill(info["abbr"], *info["colors"]),
                                   values=(player["team"], rating))

        query.trace_add("write", refresh)
        team_box.bind("<<ComboboxSelected>>", refresh)
        def change_league(_event=None):
            team_names = sorted({player["team"] for player in self.memory.players
                                 if league.get() == "全部联赛" or player["league"] == league.get()})
            team_box.configure(values=("全部球队", *team_names))
            if team.get() not in team_names:
                team.set("全部球队")
            refresh()
        league_box.bind("<<ComboboxSelected>>", change_league)
        refresh()
        ttk.Label(footer, text="整队复制会跳过来源球员；完成后可用主窗口的“撤销上次保存”恢复。",
                  foreground=P["muted"]).pack(anchor="w", pady=(8, 4))
        buttons = ttk.Frame(footer)
        buttons.pack(fill="x", pady=(8, 0))

        def apply_copy():
            try:
                if scope.get() == "team":
                    if team.get() == "全部球队":
                        raise ValueError("整队复制请先选择一支目标球队")
                    targets = [player for player in self.memory.players
                               if player["team"] == team.get() and player["index"] != source["index"]]
                else:
                    chosen = target_list.selection()
                    if not chosen:
                        raise ValueError("请从列表中选择一名目标球员")
                    targets = [candidates[chosen[0]]]
                self.status.set(f"正在将{kind.get()} DNA 复制给 {len(targets)} 名球员…")
                dialog.update_idletasks()
                backup = self.memory.copy_dna(
                    source, targets, kind.get(), advanced_fields=self.advanced_fields,
                    extra_fields=self.extra_fields, appearance_fields=self.appearance_fields,
                    source_record=self.current_edit_address)
                self.last_backup = backup
                self._refresh_selected_record()
                self.status.set(f"已复制给 {len(targets)} 名球员；可撤销。请重新打开游戏内编辑器并保存名单。")
                dialog.destroy()
            except Exception as exc:
                self.status.set("DNA 复制未完成")
                messagebox.showerror("复制 DNA 失败", str(exc), parent=dialog)

        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side="right")
        ttk.Button(buttons, text="复制到目标", style="Accent.TButton", command=apply_copy).pack(side="right", padx=8)
        dialog.bind("<Escape>", lambda _e: dialog.destroy())
        dialog.grab_set()

    def _has_pending_edits(self) -> bool:
        return bool(self.baseline) and self._refresh_dirty() > 0

    def _confirm_discard(self, action: str = "继续") -> bool:
        """Ask before unsaved edits are thrown away. Returns True to go ahead."""
        if not self._has_pending_edits():
            return True
        answer = messagebox.askyesnocancel(
            "有未保存的修改",
            f"{self.selected['name'] if self.selected else '当前球员'} 还有 {self.dirty_count} 项修改没有保存。\n\n"
            f"是：先保存再{action}\n否：放弃这些修改\n取消：留在当前页面", parent=self)
        if answer is None:
            return False
        return self.save() if answer else True

    @staticmethod
    def _profile_display(key: str, value: object) -> str:
        if key in ("position", "secondary_position"):
            return POSITIONS[value] if 0 <= value < len(POSITIONS) else str(value)
        if key == "dominant_hand":
            return HANDS[value] if 0 <= value < len(HANDS) else str(value)
        if key == "dunk_hand":
            return DUNK_HANDS[value] if 0 <= value < len(DUNK_HANDS) else str(value)
        return str(value)

    def _profile_changes(self) -> tuple[dict[int, bytes], dict[int, int]]:
        current = self.baseline["profile"]
        changes: dict[int, bytes] = {}
        words: dict[int, int] = {}
        masks: dict[int, int] = {}
        for key, offset in (("last_name", 0), ("first_name", 40),
                            ("nickname", 544), ("jersey_nickname", 584)):
            text = self.profile_inputs[key].get().strip()
            if text != current[key]:
                packed = encode_name(text) if key in ("last_name", "first_name") else encode_optional_text(text)
                changes[offset] = packed
        weight = float(self.profile_inputs["weight_kg"].get().strip())
        if abs(weight - current["weight_kg"]) >= 0.051:
            if not 30 <= weight <= 250:
                raise ValueError("体重须在 30 到 250 公斤之间")
            changes[264] = struct.pack("<f", weight * 2.2046226218)
        values: dict[str, int] = {}
        for key, (offset, shift, bits, low, high) in PROFILE_BITS.items():
            text = self.profile_inputs[key].get().strip()
            options = POSITIONS if key in ("position", "secondary_position") else (
                HANDS if key == "dominant_hand" else DUNK_HANDS if key == "dunk_hand" else None)
            value = options.index(text) if options and text in options else int(text)
            if not low <= value <= high:
                raise ValueError(f"{dict(PROFILE_UI)[key]} 须在 {low} 到 {high} 之间")
            values[key] = value
            if value == current[key]:
                continue
            if offset not in words:
                words[offset] = self.memory.u32(self.selected["address"] + offset)
            mask = ((1 << bits) - 1) << shift
            words[offset] = (words[offset] & ~mask) | (value << shift)
            masks[offset] = masks.get(offset, 0) | mask
        if any(values[key] != current[key] for key in ("birth_year", "birth_month", "birth_day")):
            date(values["birth_year"], values["birth_month"], values["birth_day"])
        if (values["peak_start"] != current["peak_start"] or values["peak_end"] != current["peak_end"]) and values["peak_end"] < values["peak_start"]:
            raise ValueError("巅峰结束年龄不能早于起始年龄")
        changes.update({offset: struct.pack("<I", word) for offset, word in words.items()})
        return changes, masks

    def _refresh_selected_record(self):
        index, uid = self.selected["index"], self.selected["uid"]
        self.memory.refresh_players()
        self.selected = next((p for p in self.memory.players if p["index"] == index and p["uid"] == uid), None)
        if self.selected is None:
            raise RuntimeError("修改后找不到原球员，请重新连接游戏。")
        self._filter()
        self.reload()

    def _make_extra_tabs(self):
        group_names = {"Jump Shooting": "跳投", "Layups And Dunks": "上篮与扣篮", "Drive Setup": "突破准备",
                       "Driving": "突破", "Passing": "传球", "Post Game": "背身", "Freelance": "自由进攻",
                       "Defense": "防守", "Hot Zones": "热区", "Inside Scoring": "篮下得分",
                       "Outside Scoring": "外线得分", "Playmaking": "组织", "Defending": "防守",
                       "Athleticism": "运动", "Rebounding": "篮板", "Personality": "个性",
                       "Flags": "标记", "Gameplay": "比赛"}
        notes = {"Tendencies": "倾向为 0–100；热区可选 冷区 / 普通 / 热区 / 极热。修改后点「保存修改」。",
                 "Badges": "徽章等级可选 未装备 / 铜 / 银 / 金 / 名人堂 / 传奇；勾选项为开关。修改后点「保存修改」。"}
        for section, title in (("Tendencies", "倾向"), ("Badges", "徽章")):
            outer = ttk.Frame(self.tabs, padding=(4, 10, 4, 4))
            self.tabs.add(outer, text=title)
            ttk.Label(outer, text=notes[section], style="Muted.TLabel").pack(anchor="w", padx=10, pady=(0, 6))
            book = ttk.Notebook(outer, style="Sub.TNotebook")
            book.pack(fill="both", expand=True)
            for group in dict.fromkeys(field["group"] for field in self.extra_fields if field["section"] == section):
                area = ScrollFrame(book, padding=(16, 12))
                book.add(area, text=group_names.get(group, group))
                pages = [(self.tabs, outer), (book, area)]
                items = [field for field in self.extra_fields if field["section"] == section and field["group"] == group]
                levels = [field for field in items if field["bits"] != 1]
                flags = [field for field in items if field["bits"] == 1]
                columns = 2
                for n, field in enumerate(levels):
                    row, column = n // columns, (n % columns) * 2
                    key = section + ":" + field["id"]
                    ttk.Label(area.inner, text=field["label"]).grid(row=row, column=column, sticky="w", pady=4,
                                                                    padx=(0, 10))
                    var = tk.StringVar()
                    self.extra_inputs[key] = var
                    choices = extra_choices(field)
                    if choices:
                        widget = ttk.Combobox(area.inner, textvariable=var, values=choices, state="readonly", width=11)
                    else:
                        widget = ttk.Entry(area.inner, textvariable=var, width=7, justify="center")
                    widget.grid(row=row, column=column + 1, sticky="w", padx=(0, 40), pady=4)
                    self._track(var, widget, pages,
                                lambda k=key, v=var: leading_int(v.get()) != self.baseline["extras"][k])
                start = (len(levels) + columns - 1) // columns
                if flags and levels:
                    ttk.Label(area.inner, text="开关", style="Section.TLabel").grid(
                        row=start, column=0, columnspan=columns * 2, sticky="w", pady=(14, 4))
                    start += 1
                for n, field in enumerate(flags):
                    key = section + ":" + field["id"]
                    var = tk.StringVar(value="0")
                    self.extra_inputs[key] = var
                    widget = ttk.Checkbutton(area.inner, text=field["label"], variable=var, onvalue="1", offvalue="0")
                    widget.grid(row=start + n // columns, column=(n % columns) * 2, columnspan=2, sticky="w",
                                pady=3, padx=(0, 40))
                    self._track(var, widget, pages,
                                lambda k=key, v=var: leading_int(v.get()) != self.baseline["extras"][k])
                area.bind_wheel()

    def _make_advanced_tab(self):
        page = ttk.Frame(self.tabs, padding=12)
        self.tabs.add(page, text="高级字段")
        ttk.Label(page, text="搜索字段后选择一项，修改其原始数值。字段按游戏数据格式显示；此页单独保存。",
                  style="Muted.TLabel").pack(anchor="w", pady=(0, 8))
        self.advanced_search = tk.StringVar()
        SearchBox(page, self.advanced_search, "搜索类别、分组或字段名…", icons=self._icon).pack(fill="x", pady=(0, 8))
        self.advanced_search.trace_add("write", lambda *_: self._advanced_refresh())
        frame = ttk.Frame(page)
        frame.pack(fill="both", expand=True)
        self.advanced_tree = ttk.Treeview(frame, columns=("section", "group", "field", "value"), show="headings")
        for key, label, width in (("section", "类别", 90), ("group", "分组", 120),
                                  ("field", "字段", 240), ("value", "当前原始值", 120)):
            self.advanced_tree.heading(key, text=label, anchor="w")
            self.advanced_tree.column(key, width=width, anchor="w")
        bar = ttk.Scrollbar(frame, orient="vertical", command=self.advanced_tree.yview)
        self.advanced_tree.configure(yscrollcommand=bar.set)
        self.advanced_tree.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self.advanced_tree.bind("<<TreeviewSelect>>", self._advanced_select)
        controls = ttk.Frame(page)
        controls.pack(fill="x", pady=(10, 0))
        self.advanced_info = tk.StringVar(value="请选择字段")
        ttk.Label(controls, textvariable=self.advanced_info).pack(side="left", padx=(0, 10))
        self.advanced_value = tk.StringVar()
        ttk.Entry(controls, textvariable=self.advanced_value, width=16, justify="center").pack(side="left")
        ttk.Button(controls, text="保存此字段", style="Accent.TButton", command=self._advanced_save).pack(
            side="left", padx=8)

    @staticmethod
    def _advanced_raw(row: bytes, field: dict) -> int | float:
        offset = field["offset"]
        if field["kind"] == "float":
            return round(struct.unpack_from("<f", row, offset)[0], 5)
        return (struct.unpack_from("<I", row, offset)[0] >> field["shift"]) & ((1 << field["bits"]) - 1)

    def _advanced_refresh(self):
        if not hasattr(self, "advanced_tree"):
            return
        self.advanced_tree.delete(*self.advanced_tree.get_children())
        if not self.memory or not self.selected:
            return
        row = self.memory.read(self.selected["address"], PLAYER_STRIDE)
        term = self.advanced_search.get().strip().casefold()
        section_names = {"Vitals": "资料", "Gear": "装备", "Stats": "数据", "Advanced": "高级",
                         "Signature": "动作", "Attributes": "属性", "Contract": "合同", "Appearance": "外观"}
        for index, field in enumerate(self.advanced_fields):
            group_label = field.get("group_label", field["group"])
            section_label = section_names.get(field["section"], field["section"])
            haystack = f"{section_label} {group_label} {field['section']} {field['group']} {field['id']} {field['label']}".casefold()
            if term and term not in haystack:
                continue
            self.advanced_tree.insert("", "end", iid=str(index), values=(
                section_label, group_label, field["label"],
                self._advanced_raw(row, field)))

    def _advanced_select(self, _event=None):
        selected = self.advanced_tree.selection()
        if not selected:
            return
        field = self.advanced_fields[int(selected[0])]
        value = self._advanced_raw(self.memory.read(self.selected["address"], PLAYER_STRIDE), field)
        self.advanced_value.set(str(value))
        range_text = "浮点数" if field["kind"] == "float" else f"0–{(1 << field['bits']) - 1}"
        self.advanced_info.set(f"{field['label']}  可填范围：{range_text}")

    def _advanced_save(self):
        selected = self.advanced_tree.selection()
        if not selected or not self.memory or not self.selected:
            self.status.set("请先选择高级字段")
            return
        if self._has_pending_edits():
            messagebox.showinfo("保存高级字段", "请先保存或重新读取其他手动修改。", parent=self)
            return
        field = self.advanced_fields[int(selected[0])]
        offset = field["offset"]
        try:
            current = self.memory.read(self.selected["address"] + offset, 4)
            if field["kind"] == "float":
                value = float(self.advanced_value.get().strip())
                if not -1e9 <= value <= 1e9:
                    raise ValueError("浮点数超出允许范围")
                packed = struct.pack("<f", value)
                masks = {}
            else:
                value = int(self.advanced_value.get().strip(), 0)
                limit = (1 << field["bits"]) - 1
                if not 0 <= value <= limit:
                    raise ValueError(f"数值须在 0 到 {limit} 之间")
                mask = limit << field["shift"]
                old_word = struct.unpack("<I", current)[0]
                packed = struct.pack("<I", (old_word & ~mask) | (value << field["shift"]))
                masks = {offset: mask}
            if packed == current:
                self.status.set("该字段没有变化")
                return
            backup = self.memory.apply(self.selected, {offset: packed}, {}, edit_record=self.current_edit_address, masks=masks)
            self.last_backup = backup
            self._refresh_selected_record()
            self.status.set(f"已修改 {field['label']}；可撤销上次保存")
        except Exception as exc:
            messagebox.showerror("保存高级字段失败", str(exc), parent=self)

    def _extra_changes(self) -> tuple[dict[int, bytes], dict[int, int]]:
        updates = []
        for field in self.extra_fields:
            key = field["section"] + ":" + field["id"]
            try:
                value = leading_int(self.extra_inputs[key].get())
            except ValueError:
                raise ValueError(f"{field['label']}：请输入数字或从列表选择") from None
            bit_limit = (1 << field["bits"]) - 1
            limit = 100 if field["section"] == "Tendencies" and field["bits"] == 7 else (
                5 if field["section"] == "Badges" and field["bits"] == 3 else bit_limit)
            if not 0 <= value <= limit:
                raise ValueError(f"{field['label']} 须在 0 到 {limit} 之间")
            if value != self.baseline["extras"][key]:
                updates.append((field, value))
        return self._pack_extra_updates(updates)

    def _signature_changes(self) -> tuple[dict[int, bytes], dict[int, int]]:
        updates = []
        for field in self.signature_fields:
            value = self._signature_value(field, self.signature_inputs[field["id"]].get())
            maximum = (1 << field["bits"]) - 1
            if not 0 <= value <= maximum:
                raise ValueError(f"{signature_label(field)} 须在 0 到 {maximum} 之间")
            if value != self.baseline["signatures"][field["id"]]:
                updates.append((field, value))
        return self._pack_extra_updates(updates)

    def _pack_extra_updates(self, updates: list[tuple[dict, int]]) -> tuple[dict[int, bytes], dict[int, int]]:
        words: dict[int, int] = {}
        masks: dict[int, int] = {}
        for field, value in updates:
            bit_limit = (1 << field["bits"]) - 1
            offset = field["offset"]
            if offset not in words:
                words[offset] = struct.unpack("<I", self.memory.read(self.selected["address"] + offset, 4))[0]
            mask = bit_limit << field["shift"]
            words[offset] = (words[offset] & ~mask) | (value << field["shift"])
            masks[offset] = masks.get(offset, 0) | mask
        return {offset: struct.pack("<I", word) for offset, word in words.items()}, masks

    def connect(self, *, quiet: bool = False) -> bool:
        if not quiet and not self._confirm_discard("重新连接"):
            return False
        if self.detect_pending:
            self._finish_detect()
        try:
            if self.memory:
                self.memory.close()
            self.memory = None
            self.memory = GameMemory()
        except Exception as exc:
            self.memory = None
            self._reset_player()
            self._set_connection(False, "未连接")
            if not quiet:
                self.status.set(str(exc))
            return False
        self.last_backup = None
        self.playbook_last_backup = None
        self.staff_last_backup = None
        self._reset_player()
        version = "" if self.memory.build_verified else " · 游戏版本未验证"
        self._set_connection(True, f"已连接 · {len(self.memory.players)} 名球员{version}")
        self.status.set("已连接游戏。从左侧选择球员，或停在游戏「编辑球员」页面后点「内存识别当前球员」。")
        if self.tabs.select() == str(self.playbook_page):
            self._refresh_playbooks()
        self._refresh_staff_teams()
        return True

    def _reset_player(self):
        self.selected = None
        self.current_edit_address = None
        self.baseline = None
        self._clear_inputs()
        self._filter()
        self._update_player_card()
        self._refresh_dirty()

    def _clear_inputs(self):
        for var in (*self.rating_inputs.values(), *self.signature_inputs.values(),
                    *self.profile_inputs.values(), self.height, self.wingspan, self.arm_scale):
            var.set("")
        flags = {field["section"] + ":" + field["id"] for field in self.extra_fields if field["bits"] == 1}
        for key, var in self.extra_inputs.items():
            var.set("0" if key in flags else "")
        self.custom_scales.set(False)
        self._advanced_refresh()

    def _watchdog(self):
        """Notice when the game closes or starts, and reconnect without a click."""
        try:
            if self.memory and not psutil.pid_exists(self.memory.pid):
                self.memory.close()
                self.memory = None
                self._reset_player()
                self._set_connection(False, "游戏已关闭")
                self.status.set("游戏已关闭。重新启动游戏后会自动连接。")
            elif not self.memory and not self.detect_pending:
                try:
                    find_game()
                except RuntimeError:
                    self._set_connection(False, "等待游戏启动…")
                else:
                    if not self.connect(quiet=True):
                        self._set_connection(False, "等待游戏载入名单…")
        except Exception as exc:
            log_error("watchdog", exc)
        finally:
            self.after(4000, self._watchdog)

    def _tab_changed(self, _event=None):
        playbook_open = self.tabs.select() == str(self.playbook_page)
        player_visible = str(self.player_panel) in self.main_panes.panes()
        if playbook_open and player_visible:
            self.main_panes.forget(self.player_panel)
        elif not playbook_open and not player_visible:
            self.main_panes.insert(0, self.player_panel, weight=0)
        if not playbook_open:
            return
        if not self.memory:
            self.connect()
        elif not getattr(self.memory, "playbooks", None):
            self._refresh_playbooks()

    def _play_meta(self, crc: int) -> dict:
        entry = self.play_catalog.get(crc)
        if entry:
            return entry
        return {"name": f"未知战术 0x{crc:08X}", "group": "其他", "detail": "未识别", "positions": []}

    @staticmethod
    def _play_position_label(positions: list[int]) -> str:
        return " / ".join(POSITION_NAMES[index - 1] for index in positions if 1 <= index <= 5) or "未标注"

    @staticmethod
    def _play_kind_label(entry: dict) -> str:
        if entry["group"] == "发球战术" and entry["detail"] != "其他发球":
            return f"发球 · {entry['detail']}"
        return entry["detail"]

    def _play_type_changed(self):
        details = GROUP_DETAILS.get(self.play_type_filter.get(), DETAIL_ORDER)
        self.play_detail_box.configure(values=("全部细分", *details))
        if self.play_detail_filter.get() not in details:
            self.play_detail_filter.set("全部细分")
        self._filter_play_catalog()

    def _filter_book_options(self):
        if not hasattr(self, "book_box"):
            return
        term = self.book_search.get().strip().casefold()
        choices = [label for label in self.book_option_map if term in label.casefold()]
        self.book_box.configure(values=choices)

    def _refresh_playbooks(self):
        if not self.memory:
            self.playbook_status.set("请先连接正在运行的 NBA 2K27。")
            return
        try:
            self.memory.refresh_playbooks()
            self.book_option_map = {
                f"{book['name']} · ID {book['id']} · #{book['index']}": book
                for book in self.memory.playbooks
            }
            self._filter_book_options()
            if self.book_choice.get() not in self.book_option_map:
                self.book_choice.set(next(iter(self.book_option_map)))
            self.play_use_count = Counter(value for book in self.memory.playbooks
                                          for value in book["slots"][:PLAYBOOK_EDITABLE_SLOTS] if value)
            self._show_playbook()
            self._filter_play_catalog()
        except Exception as exc:
            self.playbook_status.set(f"战术手册读取失败：{exc}")

    def _show_playbook(self):
        if not self.memory or not getattr(self.memory, "playbooks", None):
            return
        book = self.book_option_map.get(self.book_choice.get())
        if not book:
            return
        tree = self.play_slot_tree
        selected = tree.selection()
        selected_id = selected[0] if selected else None
        tree.delete(*tree.get_children(""))
        for slot, crc in enumerate(book["slots"]):
            entry = self._play_meta(crc) if crc else None
            label = entry["name"] if entry else "（空）"
            detail = self._play_kind_label(entry) if entry else ""
            position = self._play_position_label(entry["positions"]) if entry else ""
            suffix = " · 保留" if slot >= PLAYBOOK_EDITABLE_SLOTS else ""
            tree.insert("", "end", iid=f"slot:{slot}", text=f"{slot + 1:02d}  {label}{suffix}",
                        values=(detail, position))
        if selected_id and tree.exists(selected_id):
            tree.selection_set(selected_id)
            tree.see(selected_id)
        used = sum(bool(value) for value in book["slots"][:PLAYBOOK_EDITABLE_SLOTS])
        self.playbook_status.set(f"{book['name']}：已用 {used}/80 槽；右侧可选 {len(self.memory.play_crc_pool)} 种战术")
        self._update_playbook_preset_buttons()

    def _filter_play_catalog(self):
        if not hasattr(self, "play_catalog_tree"):
            return
        tree = self.play_catalog_tree
        selected = tree.selection()
        group_filter = self.play_type_filter.get()
        detail_filter = self.play_detail_filter.get()
        position_filter = self.play_position_filter.get()
        term = self.play_search.get().strip().casefold()
        # Keep expanded folders when only the data refreshed (e.g. after adding plays).
        filter_key = (group_filter, detail_filter, position_filter, term)
        same_filter = filter_key == getattr(self, "_play_filter_key", None)
        opened = set()
        if same_filter:
            opened = {node for group in tree.get_children("") for node in (group, *tree.get_children(group))
                      if tree.item(node, "open")}
        self._play_filter_key = filter_key
        tree.delete(*tree.get_children(""))
        self.play_catalog_items = {}
        if not self.memory or not getattr(self.memory, "play_crc_pool", None):
            return
        grouped: dict[str, dict[str, list[tuple[int, dict]]]] = {}
        for crc in self.memory.play_crc_pool:
            entry = self._play_meta(crc)
            if group_filter != "全部打法" and entry["group"] != group_filter:
                continue
            if detail_filter != "全部细分" and entry["detail"] != detail_filter:
                continue
            positions = entry["positions"]
            if position_filter == "未标注" and positions:
                continue
            if position_filter in POSITION_NAMES and POSITION_NAMES.index(position_filter) + 1 not in positions:
                continue
            if term and term not in f"{entry['name']} {crc:08X} {entry['group']} {entry['detail']}".casefold():
                continue
            grouped.setdefault(entry["group"], {}).setdefault(entry["detail"], []).append((crc, entry))
        rank = {detail: index for index, detail in enumerate(DETAIL_ORDER)}
        narrowed = bool(term) or detail_filter != "全部细分"
        for group in TYPE_ORDER:
            details = grouped.get(group)
            if not details:
                continue
            group_id = f"play-group:{group}"
            total = sum(len(plays) for plays in details.values())
            tree.insert("", "end", iid=group_id, text=f"{group}（{total}）",
                        open=group_id in opened or narrowed or group_filter == group)
            for detail in sorted(details, key=lambda name: rank.get(name, len(rank))):
                plays = details[detail]
                parent = group_id
                if len(GROUP_DETAILS[group]) > 1:
                    parent = f"play-detail:{group}:{detail}"
                    tree.insert(group_id, "end", iid=parent, text=f"{detail}（{len(plays)}）",
                                open=parent in opened or narrowed)
                for crc, entry in sorted(plays, key=lambda item: (item[1]["name"], item[0])):
                    item_id = f"play:{crc:08X}"
                    tree.insert(parent, "end", iid=item_id, text=entry["name"],
                                values=(self._play_kind_label(entry), self._play_position_label(entry["positions"]),
                                        self.play_use_count.get(crc, 0)))
                    self.play_catalog_items[item_id] = crc
        kept = [item for item in selected if tree.exists(item)]
        if kept:
            tree.selection_set(*kept)
            tree.see(kept[0])
        elif not same_filter:
            tree.yview_moveto(0)

    def _change_playbook(self, action: str):
        try:
            if not self.memory:
                raise RuntimeError("请先连接游戏。")
            book = self.book_option_map.get(self.book_choice.get())
            if not book:
                raise ValueError("请先选择一本战术手册。")
            if action == "add":
                slot = next((index for index, crc in enumerate(book["slots"][:PLAYBOOK_EDITABLE_SLOTS]) if not crc), None)
                if slot is None:
                    raise ValueError("当前手册前 80 个槽位已满；请选择一个槽位替换。")
            else:
                chosen = self.play_slot_tree.selection()
                if len(chosen) != 1:
                    raise ValueError("单项操作请只选一个左侧槽位；多选请使用批量删除。")
                slot = int(chosen[0].split(":", 1)[1])
            if action == "clear":
                crc = 0
            else:
                chosen = self.play_catalog_tree.selection()
                crc = self.play_catalog_items.get(chosen[0]) if len(chosen) == 1 else None
                if crc is None:
                    raise ValueError("单项操作请只选一个右侧战术；多选请使用批量添加。")
            backup = self.memory.replace_playbook_slot(book, slot, crc)
            self.playbook_last_backup = backup
            self._refresh_playbooks()
            self.play_slot_tree.selection_set(f"slot:{slot}")
            self.play_slot_tree.see(f"slot:{slot}")
            self.playbook_status.set(f"已修改 {book['name']} 的第 {slot + 1} 槽；请保存当前游戏存档或名单。")
        except Exception as exc:
            messagebox.showerror("战术修改失败", str(exc), parent=self)

    def _batch_add_playbook(self):
        try:
            if not self.memory:
                raise RuntimeError("请先连接游戏。")
            book = self.book_option_map.get(self.book_choice.get())
            if not book:
                raise ValueError("请先选择一本战术手册。")
            selected = set(self.play_catalog_tree.selection())
            crcs = [crc for item, crc in self.play_catalog_items.items() if item in selected]
            if not crcs:
                raise ValueError("请按住 Ctrl/Shift 在右侧多选要加入的战术。")
            empty = [slot for slot, crc in enumerate(book["slots"][:PLAYBOOK_EDITABLE_SLOTS]) if not crc]
            if len(crcs) > len(empty):
                raise ValueError(f"选择了 {len(crcs)} 个战术，但只有 {len(empty)} 个空槽；请减少选择或先清空槽位。")
            updates = dict(zip(empty, crcs))
            backup = self.memory.edit_playbook_slots(book, updates)
            self.playbook_last_backup = backup
            self._refresh_playbooks()
            self.play_slot_tree.selection_set(*(f"slot:{slot}" for slot in updates))
            self.play_slot_tree.see(f"slot:{next(iter(updates))}")
            self.playbook_status.set(f"已批量添加 {len(updates)} 个战术到 {book['name']}；可一次撤销。")
        except Exception as exc:
            messagebox.showerror("批量添加战术失败", str(exc), parent=self)

    def _batch_clear_playbook(self):
        try:
            if not self.memory:
                raise RuntimeError("请先连接游戏。")
            book = self.book_option_map.get(self.book_choice.get())
            if not book:
                raise ValueError("请先选择一本战术手册。")
            selected = sorted(int(item.split(":", 1)[1]) for item in self.play_slot_tree.selection()
                              if item.startswith("slot:"))
            if not selected:
                raise ValueError("请按住 Ctrl/Shift 在左侧选择要删除的战术槽位。")
            if any(slot >= PLAYBOOK_EDITABLE_SLOTS for slot in selected):
                raise ValueError("所选项目包含 81–88 的保留槽位；请只选择前 80 个标准槽位。")
            updates = {slot: 0 for slot in selected if book["slots"][slot]}
            if not updates:
                raise ValueError("所选槽位均为空，无需删除。")
            backup = self.memory.edit_playbook_slots(book, updates)
            self.playbook_last_backup = backup
            self._refresh_playbooks()
            self.play_slot_tree.selection_set(*(f"slot:{slot}" for slot in updates))
            self.play_slot_tree.see(f"slot:{next(iter(updates))}")
            self.playbook_status.set(f"已批量删除 {len(updates)} 个槽位的战术；可一次撤销。")
        except Exception as exc:
            messagebox.showerror("批量删除战术失败", str(exc), parent=self)

    def _undo_playbook(self):
        if not self.memory or not self.playbook_last_backup:
            self.playbook_status.set("没有可撤销的本次战术修改。")
            return
        try:
            self.memory.undo(self.playbook_last_backup)
            self.playbook_last_backup = None
            self._refresh_playbooks()
            self.playbook_status.set("已撤销上次战术修改。")
        except Exception as exc:
            messagebox.showerror("撤销战术失败", str(exc), parent=self)

    # Presets: named 动作 values and playbook slot lists, kept in PRESET_FILE.
    def _refresh_preset_boxes(self):
        for kind, box in (("signatures", self.signature_preset_box), ("playbooks", self.playbook_preset_box)):
            newest = sorted(self.presets[kind].items(), key=lambda item: str(item[1].get("saved", "")), reverse=True)
            box.configure(values=[name for name, _preset in newest])

    def _preset_var(self, kind: str) -> tk.StringVar:
        return self.signature_preset_name if kind == "signatures" else self.playbook_preset_name

    def _preset_name(self, kind: str, default: str) -> str | None:
        """Typed name (or the default) to save under; None when the user keeps the old preset."""
        name = self._preset_var(kind).get().strip() or default
        if len(name) > 40:
            raise ValueError("预设名称最多 40 个字。")
        if name in self.presets[kind] and not messagebox.askyesno(
                "覆盖预设", f"预设「{name}」已存在，要用当前内容覆盖吗？", parent=self):
            return None
        return name

    def _store_preset(self, kind: str, name: str, preset: dict | None):
        data = load_presets()  # re-read so presets saved by another open copy survive
        if preset is None:
            data[kind].pop(name, None)
        else:
            data[kind][name] = {**preset, "saved": time.strftime("%Y-%m-%d %H:%M")}
        save_presets(data)
        self.presets = data
        self._refresh_preset_boxes()
        self._preset_var(kind).set("" if preset is None else name)

    def _delete_preset(self, kind: str):
        title = "动作预设" if kind == "signatures" else "手册预设"
        name = self._preset_var(kind).get().strip()
        try:
            if name not in self.presets[kind]:
                raise ValueError(f"请先从列表选择要删除的{title}。")
            if not messagebox.askyesno(f"删除{title}", f"删除{title}「{name}」？游戏里的数据不受影响。", parent=self):
                return
            self._store_preset(kind, name, None)
            self.status.set(f"已删除{title}「{name}」")
        except (ValueError, OSError) as exc:
            messagebox.showerror(f"删除{title}失败", str(exc), parent=self)

    def _signature_scope_fields(self) -> tuple[str, list[dict]]:
        scope = self.signature_preset_scope.get()
        if scope == ALL_SIGNATURES:
            return scope, self.signature_fields
        group = next((key for key, title in SIGNATURE_GROUPS.items() if title == scope), None)
        return scope, [field for field in self.signature_fields if field["group"] == group]

    def _show_signature_preset_info(self):
        name = self.signature_preset_name.get().strip()
        preset = self.presets["signatures"].get(name)
        if not preset:
            self.signature_preset_info.set(
                f"输入名称（留空则用球员名）后点「保存为预设」，保存当前球员的{self.signature_preset_scope.get()}；"
                "载入预设只填入下方输入框，点「保存修改」才写入游戏。")
            return
        values = preset.get("values", {})
        counts = Counter(SIGNATURE_GROUPS.get(field["group"], field["group"])
                         for field in self.signature_fields if field["id"] in values)
        content = (f"全部 {sum(counts.values())} 项" if len(counts) == len(SIGNATURE_GROUPS)
                   else "，".join(f"{title} {count} 项" for title, count in counts.items()) or "没有可用的动作")
        self.signature_preset_info.set(
            f"预设「{name}」：{content} · 来源 {preset.get('source', '未知')} · {preset.get('saved', '')}")

    def _save_signature_preset(self):
        try:
            if not self.baseline or not self.selected:
                raise ValueError("请先选择或识别一名球员，再把该球员的动作保存为预设。")
            scope, fields = self._signature_scope_fields()
            values = {}
            for field in fields:
                value = self._signature_value(field, self.signature_inputs[field["id"]].get())
                maximum = (1 << field["bits"]) - 1
                if not 0 <= value <= maximum:
                    raise ValueError(f"{signature_label(field)} 须在 0 到 {maximum} 之间")
                values[field["id"]] = value
            player = self.selected["name"]
            name = self._preset_name("signatures", player if scope == ALL_SIGNATURES else f"{player} · {scope}")
            if name is None:
                return
            self._store_preset("signatures", name, {"source": player, "scope": scope, "values": values})
            self.status.set(f"已保存动作预设「{name}」：{scope} {len(values)} 项")
        except (ValueError, OSError) as exc:
            messagebox.showerror("保存动作预设失败", str(exc), parent=self)

    def _load_signature_preset(self):
        try:
            if not self.baseline:
                raise ValueError("请先选择或识别一名球员，再载入动作预设。")
            name = self.signature_preset_name.get().strip()
            preset = self.presets["signatures"].get(name)
            if not preset:
                raise ValueError("请从列表选择一个已保存的动作预设。")
            scope, fields = self._signature_scope_fields()
            values = preset.get("values", {})
            loaded = changed = skipped = 0
            for field in fields:
                value = values.get(field["id"])
                if value is None:
                    continue
                if not isinstance(value, int) or not 0 <= value < 1 << field["bits"]:
                    skipped += 1
                    continue
                self.signature_inputs[field["id"]].set(self._signature_display(field, value))
                loaded += 1
                changed += value != self.baseline["signatures"][field["id"]]
            if not loaded:
                raise ValueError(f"预设「{name}」里没有「{scope}」范围的动作。")
            self._refresh_dirty()
            note = f"，{skipped} 项数值无效已跳过" if skipped else ""
            self.status.set(f"已载入「{name}」{loaded} 项（{changed} 项有变化{note}），点「保存修改」写入游戏")
        except ValueError as exc:
            messagebox.showerror("载入动作预设失败", str(exc), parent=self)

    def _selected_book(self) -> dict:
        if not self.memory:
            raise RuntimeError("请先连接游戏。")
        book = self.book_option_map.get(self.book_choice.get())
        if not book:
            raise ValueError("请先选择一本战术手册。")
        return book

    def _selected_play_slots(self, book: dict) -> list[int]:
        """Selected left-side slots that hold a play and may be edited."""
        slots = []
        for item in self.play_slot_tree.selection():
            slot = int(item.split(":", 1)[1])
            if slot < PLAYBOOK_EDITABLE_SLOTS and book["slots"][slot]:
                slots.append(slot)
        return sorted(slots)

    def _update_playbook_preset_buttons(self):
        book = self.book_option_map.get(self.book_choice.get())
        chosen = bool(book) and bool(self._selected_play_slots(book))
        self.playbook_save_selected.state(["!disabled"] if chosen else ["disabled"])

    def _preset_play(self, crc: int) -> dict:
        return {"crc": f"{crc:08X}", "name": self._play_meta(crc)["name"]}

    def _save_playbook_preset(self, *, selected_only: bool):
        try:
            book = self._selected_book()
            slots = book["slots"][:PLAYBOOK_EDITABLE_SLOTS]
            if selected_only:
                chosen = self._selected_play_slots(book)
                if not chosen:
                    raise ValueError("请先在左侧选择要保存的战术（空槽和 81–88 保留槽不会保存）。")
                preset = {"whole": False, "plays": [self._preset_play(slots[slot]) for slot in chosen]}
                count, default = len(chosen), f"{book['name']} · {len(chosen)} 个战术"
            else:
                count = sum(1 for crc in slots if crc)
                if not count:
                    raise ValueError("当前手册前 80 槽都是空的，没有可保存的战术。")
                preset = {"whole": True, "slots": [self._preset_play(crc) if crc else None for crc in slots]}
                default = book["name"]
            name = self._preset_name("playbooks", default)
            if name is None:
                return
            self._store_preset("playbooks", name, {**preset, "source": book["name"]})
            self.playbook_status.set(f"已保存手册预设「{name}」：{'所选' if selected_only else '整本'} {count} 个战术")
        except (ValueError, RuntimeError, OSError) as exc:
            messagebox.showerror("保存手册预设失败", str(exc), parent=self)

    def _show_playbook_preset_info(self):
        name = self.playbook_preset_name.get().strip()
        preset = self.presets["playbooks"].get(name)
        if preset:
            items = preset.get("slots") if preset.get("whole") else preset.get("plays")
            count = sum(1 for item in items or () if item)
            kind = "整本手册" if preset.get("whole") else "战术包"
            self.playbook_status.set(f"预设「{name}」：{kind} · {count} 个战术 · 来源 {preset.get('source', '未知')}"
                                     f" · {preset.get('saved', '')}")

    @staticmethod
    def _read_playbook_preset(preset: dict, known: frozenset[int]) -> tuple[list[int], int]:
        """Slot-ordered play CRCs (0 = empty) and how many listed plays this game does not have."""
        crcs, missing = [], 0
        for item in (preset.get("slots") if preset.get("whole") else preset.get("plays")) or ():
            try:
                crc = int(item["crc"], 16) if item else 0
            except (KeyError, TypeError, ValueError):
                crc = -1
            if crc and crc not in known:
                missing += 1
                crc = 0
            crcs.append(crc)
        return crcs, missing

    def _apply_playbook_preset(self, mode: str):
        try:
            book = self._selected_book()
            name = self.playbook_preset_name.get().strip()
            preset = self.presets["playbooks"].get(name)
            if not preset:
                raise ValueError("请从列表选择一个已保存的手册预设。")
            known = frozenset(self.play_catalog) | self.memory.play_crc_pool
            crcs, missing = self._read_playbook_preset(preset, known)
            current = list(book["slots"][:PLAYBOOK_EDITABLE_SLOTS])
            skipped = [f"{missing} 个本游戏没有的战术"] if missing else []
            if mode == "replace":
                plays = crcs if preset.get("whole") else [crc for crc in crcs if crc]
                target = (plays + [0] * PLAYBOOK_EDITABLE_SLOTS)[:PLAYBOOK_EDITABLE_SLOTS]
                updates = {slot: crc for slot, crc in enumerate(target) if current[slot] != crc}
                if not updates:
                    self.playbook_status.set(f"「{book['name']}」已与预设「{name}」一致，无需修改。")
                    return
                if not messagebox.askyesno(
                        "替换当前手册", f"用预设「{name}」替换「{book['name']}」的前 80 槽？\n\n"
                        f"将改动 {len(updates)} 个槽位，81–88 保留槽不变；可用「撤销上次战术修改」恢复。", parent=self):
                    return
                verb = "替换了"
            else:
                present, plays, duplicates = set(current), [], 0
                for crc in crcs:
                    if not crc:
                        continue
                    if crc in present:
                        duplicates += 1
                        continue
                    present.add(crc)
                    plays.append(crc)
                if duplicates:
                    skipped.append(f"{duplicates} 个已在手册中的战术")
                if not plays:
                    raise ValueError("没有可追加的战术：" + "、".join(skipped or ["预设是空的"]) + "。")
                empty = [slot for slot, crc in enumerate(current) if not crc]
                if len(plays) > len(empty):
                    raise ValueError(f"需要 {len(plays)} 个空槽，当前手册只有 {len(empty)} 个；请先清空部分槽位。")
                updates = dict(zip(empty, plays))
                verb = "追加了"
            backup = self.memory.edit_playbook_slots(book, updates, known=known)
            self.playbook_last_backup = backup
            self._refresh_playbooks()
            self.play_slot_tree.selection_set(*(f"slot:{slot}" for slot in updates))
            self.play_slot_tree.see(f"slot:{min(updates)}")
            note = f"；已跳过{'、'.join(skipped)}" if skipped else ""
            self.playbook_status.set(f"已用预设「{name}」{verb} {len(updates)} 个槽位{note}；可一次撤销。")
        except Exception as exc:
            messagebox.showerror("载入手册预设失败", str(exc), parent=self)

    def _filter(self):
        if self._search_job is not None:
            self.after_cancel(self._search_job)
            self._search_job = None
        tree = self.player_tree
        open_nodes = {}
        for league_id in tree.get_children(""):
            open_nodes[league_id] = bool(tree.item(league_id, "open"))
            for team_id in tree.get_children(league_id):
                open_nodes[team_id] = bool(tree.item(team_id, "open"))
        if not self._last_search:
            self._tree_open_state.update(open_nodes)
        tree.delete(*tree.get_children(""))
        self.player_items = {}
        if not self.memory:
            self.count_text.set("未连接游戏")
            return
        term = self.search.get().strip().casefold()
        self._last_search = term
        league_choice = self.league_filter.get()
        teams = sorted({player["team"] for player in self.memory.players
                        if league_choice == "全部联赛" or player["league"] == league_choice})
        self.team_box.configure(values=("全部球队", *teams))
        if self.team_filter.get() not in teams:
            self.team_filter.set("全部球队")
        team_choice = self.team_filter.get()
        grouped: dict[str, dict[str, list[dict]]] = {}
        total = 0
        for player in self.memory.players:
            if league_choice != "全部联赛" and player["league"] != league_choice:
                continue
            if team_choice != "全部球队" and player["team"] != team_choice:
                continue
            if term and not self._matches(player, term):
                continue
            grouped.setdefault(player["league"], {}).setdefault(player["team"], []).append(player)
            total += 1

        selected_id = None
        for league in LEAGUE_ORDER:
            team_groups = grouped.get(league, {})
            if not team_groups and not (not term and league == "WNBA" and
                                         league_choice in ("全部联赛", "WNBA")):
                continue
            count = sum(len(players) for players in team_groups.values())
            league_id = f"league:{league}"
            tree.insert("", "end", iid=league_id, text=f"  {league}", values=("", f"{count}人"),
                        image=self.badges.league(league),
                        open=term != "" or self._tree_open_state.get(league_id, league == "NBA"),
                        tags=("league",) if count else ("league", "empty"))
            for team in sorted(team_groups):
                players = team_groups[team]
                info = self.memory.team_info(players[0])
                primary, secondary = info["colors"]
                team_id = f"team:{league}:{team}"
                tree.insert(league_id, "end", iid=team_id, text=f"  {team}", values=("", f"{len(players)}人"),
                            image=self.badges.pill(info["abbr"], primary, secondary),
                            open=term != "" or self._tree_open_state.get(team_id, False),
                            tags=(self._team_tag(primary),))
                dot = self.badges.dot(primary, secondary)
                for player in sorted(players, key=lambda item: item["index"]):
                    player_id = f"player:{player['index']}"
                    rating = player["overall"] if player["overall"] is not None else "—"
                    tree.insert(team_id, "end", iid=player_id, text=f"  {player['name']}", image=dot,
                                values=(rating, player["index"]))
                    self.player_items[player_id] = player
                    if self.selected and player["index"] == self.selected["index"] and player["uid"] == self.selected["uid"]:
                        selected_id = player_id
        shown = f"显示 {total} 名球员" if total != len(self.memory.players) else f"共 {total} 名球员"
        self.count_text.set(shown + (f" · 搜索“{self.search.get().strip()}”" if term else ""))
        if selected_id:
            parent = tree.parent(selected_id)
            tree.item(parent, open=True)
            tree.item(tree.parent(parent), open=True)
            tree.selection_set(selected_id)
            tree.focus(selected_id)
            tree.see(selected_id)

    def _team_tag(self, primary: str) -> str:
        """Tint team rows with the team's own colour so rosters stand apart."""
        tag = f"team{primary}"
        if not hasattr(self, "_team_tags"):
            self._team_tags = set()
        if tag not in self._team_tags:
            self.player_tree.tag_configure(tag, background=mix(P["field"], primary, 0.24), font=theme.FONT_BOLD)
            self._team_tags.add(tag)
        return tag

    @staticmethod
    def _matches(player: dict, term: str) -> bool:
        if term in player["name"].casefold() or term == str(player["index"]):
            return True
        if term == player["team_abbr"].casefold():
            return True
        return len(term) >= 3 and term in player["team"].casefold()

    def _select(self, _event=None):
        selected = self.player_tree.selection()
        if not selected:
            return
        player = self.player_items.get(selected[0])
        if not player:
            return
        current = self.selected
        if current and player["index"] == current["index"] and player["uid"] == current["uid"]:
            return
        if not self._confirm_discard("切换球员"):
            self._reveal_selected()
            return
        self.selected = player
        self.current_edit_address = None
        self.reload()
        self._reveal_selected()

    def _reveal_selected(self):
        tree = self.player_tree
        item = f"player:{self.selected['index']}" if self.selected else None
        if item and tree.exists(item):
            if tree.selection() != (item,):
                tree.selection_set(item)
            tree.see(item)
        elif tree.selection():
            tree.selection_remove(*tree.selection())

    def detect_current(self, *, deep: bool = False):
        if self.detect_pending:
            return
        if not self.memory:
            self.connect()
        if not self.memory:
            return
        self.detect_pending = True
        self.detect_memory = self.memory
        self._set_busy(True, deep)
        self.status.set("正在全面扫描游戏内存，约需一分钟…" if deep else "正在扫描游戏内存…")
        self.after(80, lambda: self._start_detect(deep))

    def _start_detect(self, deep: bool):
        if not self.detect_pending:
            return
        try:
            context = mp.get_context("spawn")
            receiver, sender = context.Pipe(duplex=False)
            process = context.Process(target=detect_current_worker, args=(sender, deep), daemon=True)
            process.start()
            sender.close()
        except Exception as exc:
            log_error("start detect", exc)
            self._finish_detect()
            self.status.set(f"无法启动识别：{exc}")
            return
        self.detect_process, self.detect_receiver = process, receiver
        self.detect_started = time.monotonic()
        self.after(100, lambda: self._poll_detect(deep))

    def _poll_detect(self, deep: bool):
        process, receiver = self.detect_process, self.detect_receiver
        if process is None or receiver is None:
            return
        result = None
        try:
            if receiver.poll():
                result = receiver.recv()
            elif process.exitcode is not None:
                result = {"error": f"识别进程意外退出（代码 {process.exitcode}）"}
        except (EOFError, OSError):  # The worker died without answering.
            result = {"error": f"识别进程意外退出（代码 {process.exitcode}）"}
        elapsed = time.monotonic() - self.detect_started
        if result is None and elapsed > (180 if deep else 45):
            result = {"error": "识别超时，请从左侧名单选择球员。"}
        if result is None:
            self.busy_text.set(f"{'全面内存扫描' if deep else '内存扫描中'} {elapsed:.0f} 秒")
            self.after(150, lambda: self._poll_detect(deep))
            return
        memory = self.detect_memory
        self._finish_detect()
        self._handle_detect_result(memory, result)

    def _stop_detect(self):
        if self.detect_process is not None:
            if self.detect_process.is_alive():
                self.detect_process.terminate()
            self.detect_process.join(timeout=0.2)
            self.detect_process = None
        if self.detect_receiver is not None:
            self.detect_receiver.close()
            self.detect_receiver = None

    def _finish_detect(self):
        self.detect_pending = False
        self._stop_detect()
        self._set_busy(False)

    def _cancel_detect(self):
        if self.detect_pending:
            self._finish_detect()
            self.status.set("已取消识别。")

    def _set_busy(self, busy: bool, deep: bool = False):
        for button in (self.detect_button, self.deep_button, self.reconnect_button):
            button.state(["disabled"] if busy else ["!disabled"])
        if busy:
            self.busy_text.set("全面内存扫描…" if deep else "内存扫描中…")
            self.cancel_detect_button.pack(side="right", padx=(12, 8))
            self.busy_bar.pack(side="right", padx=(10, 0))
            self.busy_label.pack(side="right")
            self.busy_bar.start(12)
        else:
            self.busy_bar.stop()
            for widget in (self.cancel_detect_button, self.busy_bar, self.busy_label):
                widget.pack_forget()

    def _handle_detect_result(self, memory: GameMemory | None, result: dict):
        if memory is None or self.memory is not memory:
            return
        if "error" in result:
            self.status.set(result["error"])
            return
        if result.get("pid") != memory.pid:
            self.status.set("游戏已重启，请重新连接。")
            return
        player = next((p for p in memory.players if p["index"] == result.get("index")
                       and p["uid"] == result.get("uid")), None)
        if not player:
            self.status.set("内存中没有找到正在编辑的球员。请让游戏停在「编辑球员」页面后再试，或从左侧名单选择。")
            return
        self._detected(memory, (player, result.get("address")), None)

    def _detected(self, memory: GameMemory, found: tuple[dict, int] | None, error: str | None):
        if self.memory is not memory:
            return
        if error:
            self.status.set(error)
            return
        if not found:
            self.status.set("没有找到正在打开的球员编辑器；可从左侧名单选择。")
            return
        player, address = found
        current = self.selected
        same = current and player["index"] == current["index"] and player["uid"] == current["uid"]
        if not same and not self._confirm_discard("切换到识别到的球员"):
            self.status.set(f"识别到 {player['name']}，已保留当前未保存的修改。")
            return
        self.selected = player
        self.current_edit_address = address
        self.league_filter.set(player["league"])
        self.team_filter.set(player["team"])
        self.search.set("")
        self._filter()
        self.reload()
        self._reveal_selected()
        source = "游戏编辑器内存"
        self.status.set(f"已从{source}识别：{player['name']} · {player['team']}（保存前请核对姓名）")

    def _reload_clicked(self):
        if not self.selected:
            return
        if self._has_pending_edits() and not messagebox.askyesno(
                "重新读取", "放弃未保存的修改，并从游戏重新读取当前球员？", parent=self):
            return
        self.reload()

    def reload(self):
        if not self.memory or not self.selected:
            return
        try:
            snap = self.memory.player_snapshot(self.selected, self.fields, record_address=self.current_edit_address,
                                               extra_fields=self.extra_fields,
                                               signature_fields=self.signature_fields)
            self.baseline = snap
            self.height.set(str(snap["height_cm"]))
            self.wingspan.set(str(snap["wingspan_cm"]))
            self.arm_scale.set(str(snap["arm_scale"]))
            self.custom_scales.set(snap["custom_scales"])
            for key, _label in PROFILE_UI:
                self.profile_inputs[key].set(self._profile_display(key, snap["profile"][key]))
            for field in self.fields:
                self.rating_inputs[field["id"]].set(str(snap["ratings"][field["id"]]))
            extra_by_key = {field["section"] + ":" + field["id"]: field for field in self.extra_fields}
            for key, value in snap["extras"].items():
                self.extra_inputs[key].set(extra_display(extra_by_key[key], value))
            signature_by_id = {field["id"]: field for field in self.signature_fields}
            for key, value in snap["signatures"].items():
                self.signature_inputs[key].set(self._signature_display(signature_by_id[key], value))
            self._advanced_refresh()
            self._refresh_dirty()
            self._update_player_card()
            self.status.set(f"已读取 {self.selected['name']} 的数据")
        except Exception as exc:
            self.status.set(str(exc))

    def save(self) -> bool:
        """Write pending edits. Returns True when nothing is left unsaved."""
        if not self.memory or not self.selected or not self.baseline:
            return True
        try:
            changes: dict[int, bytes] = {}
            for field in self.fields:
                try:
                    value = int(self.rating_inputs[field["id"]].get().strip())
                except ValueError:
                    raise ValueError(f"{field['label']}：请输入 {RATING_MIN} 到 {RATING_MAX} 的整数") from None
                if not RATING_MIN <= value <= RATING_MAX:
                    raise ValueError(f"{field['label']} 须在 {RATING_MIN} 到 {RATING_MAX} 之间")
                if value != self.baseline["ratings"][field["id"]]:
                    changes[field["offset"]] = bytes([rating_to_raw(value)])
            extra_changes, masks = self._extra_changes()
            changes.update(extra_changes)
            profile_changes, profile_masks = self._profile_changes()
            for offset, value in profile_changes.items():
                if offset in changes and offset in masks and offset in profile_masks:
                    old_word = self.memory.u32(self.selected["address"] + offset)
                    existing_word = struct.unpack("<I", changes[offset])[0]
                    profile_word = struct.unpack("<I", value)[0]
                    if masks[offset] & profile_masks[offset]:
                        raise RuntimeError("两个修改项使用了重叠的数据位，已停止保存。")
                    value = struct.pack("<I", (old_word & ~(masks[offset] | profile_masks[offset])) |
                                        (existing_word & masks[offset]) | (profile_word & profile_masks[offset]))
                elif offset in changes:
                    raise RuntimeError("两个修改项使用了重叠的地址，已停止保存。")
                changes[offset] = value
            masks.update({offset: masks.get(offset, 0) | mask for offset, mask in profile_masks.items()})
            signature_changes, signature_masks = self._signature_changes()
            for offset, value in signature_changes.items():
                if offset in changes:
                    if offset not in masks or len(changes[offset]) != 4 or masks[offset] & signature_masks[offset]:
                        raise RuntimeError("动作与其他修改项使用了重叠的数据位，已停止保存。")
                    old_word = self.memory.u32(self.selected["address"] + offset)
                    existing_word = struct.unpack("<I", changes[offset])[0]
                    signature_word = struct.unpack("<I", value)[0]
                    combined_mask = masks[offset] | signature_masks[offset]
                    value = struct.pack("<I", (old_word & ~combined_mask) |
                                        (existing_word & masks[offset]) |
                                        (signature_word & signature_masks[offset]))
                changes[offset] = value
                masks[offset] = masks.get(offset, 0) | signature_masks[offset]
            try:
                height_cm = float(self.height.get().strip())
                wingspan_cm = float(self.wingspan.get().strip())
                arm_scale = float(self.arm_scale.get().strip())
            except ValueError:
                raise ValueError("身体页的身高、臂展和手臂比例须为数字") from None
            if not 50 <= height_cm <= 327.67:
                raise ValueError("身高须在 50 到 327.67 厘米之间")
            if not 50 <= wingspan_cm <= 327.67:
                raise ValueError("臂展须在 50 到 327.67 厘米之间")
            if not 0.5 <= arm_scale <= 2.0:
                raise ValueError("手臂模型比例须在 0.5 到 2.0 之间")
            body = {}
            if abs(height_cm - self.baseline["height_cm"]) > 0.005:
                old_word = struct.unpack("<H", self.memory.read(self.baseline["appearance"], 2))[0]
                raw = round(height_cm * 100)
                body[0] = struct.pack("<H", (old_word & 0x8000) | raw)
            if abs(wingspan_cm - self.baseline["wingspan_cm"]) > 0.005:
                old_word = struct.unpack("<H", self.memory.read(self.baseline["appearance"] + 2, 2))[0]
                body[2] = struct.pack("<H", (old_word & 0x8000) | round(wingspan_cm * 100))
            if abs(arm_scale - self.baseline["arm_scale"]) > 0.00005:
                body[12] = struct.pack("<f", arm_scale)
                self.custom_scales.set(True)
            if self.custom_scales.get() != self.baseline["custom_scales"]:
                old_word = self.memory.u32(self.baseline["appearance"] + 48)
                mask = 1 << 30
                body[48] = struct.pack("<I", (old_word & ~mask) | (mask if self.custom_scales.get() else 0))
            if not changes and not body:
                self.status.set("没有需要保存的变化")
                return True
            backup = self.memory.apply(self.selected, changes, body, edit_record=self.current_edit_address, masks=masks)
            self.last_backup = backup
            self._refresh_selected_record()
            self.status.set(f"已保存 {len(changes) + len(body)} 处修改；可撤销。备份：{backup.name}")
            return True
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc), parent=self)
            return False

    def undo(self):
        if not self.memory or not self.last_backup:
            self.status.set("没有可撤销的本次修改")
            return
        if not messagebox.askyesno("撤销上次保存", "把上次保存写入游戏的数值恢复成保存前的样子？", parent=self):
            return
        try:
            self.memory.undo(self.last_backup)
            self.last_backup = None
            self._refresh_selected_record()
            self.status.set("已撤销上次保存")
        except Exception as exc:
            messagebox.showerror("撤销失败", str(exc), parent=self)

    def _quit(self):
        if not self._confirm_discard("退出"):
            return
        zoomed = self.state() == "zoomed"
        self.settings["zoomed"] = zoomed
        if not zoomed:
            self.settings["geometry"] = self.geometry()
        save_settings(self.settings)
        if self.detect_pending:
            self._finish_detect()
        if self.memory:
            self.memory.close()
        self.destroy()


if __name__ == "__main__":
    mp.freeze_support()
    if len(sys.argv) == 3 and sys.argv[1] == "--probe-detect":
        receiver, sender = mp.get_context("spawn").Pipe(duplex=False)
        process = mp.get_context("spawn").Process(target=detect_current_worker, args=(sender, False))
        process.start()
        sender.close()
        result = receiver.recv() if receiver.poll(30) else {"error": "识别超时"}
        if process.is_alive():
            process.terminate()
        process.join(timeout=1)
        Path(sys.argv[2]).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        receiver.close()
    else:
        set_dpi_awareness()
        try:
            PlayerEditor().mainloop()
        except Exception as exc:
            log_error("启动失败", exc)
            raise
