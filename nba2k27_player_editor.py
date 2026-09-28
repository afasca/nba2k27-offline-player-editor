"""Local NBA 2K27 roster editor for the inspected offline game build.

The program edits the loaded roster in memory. It does not patch game code.
"""
from __future__ import annotations

import asyncio
import ctypes as ct
from datetime import date
from io import BytesIO
import json
import multiprocessing as mp
import os
from pathlib import Path
import re
import struct
import sys
import time
import tkinter as tk
from tkinter import messagebox, ttk
from collections import Counter
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import psutil
from PIL import ImageGrab
from winrt.windows.graphics.imaging import BitmapDecoder
from winrt.windows.media.ocr import OcrEngine
from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

from playbook_catalog import POSITION_NAMES, TYPE_ORDER, classify_play


GAME_PATH = r"F:\game\NBA2K27\NBA2K27.exe"
ROOT_RVA = 161861320
PLAYER_STRIDE = 1272
PLAYBOOK_STRIDE = 536
PLAYBOOK_SLOTS = 88
PLAYBOOK_EDITABLE_SLOTS = 80
BODY_RATIO = Decimal("1.4")
BODY_MIN_CM = Decimal("50")
BODY_MAX_CM = Decimal("327.67")
FIELD_FILE = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "player_fields.json"
EXTRA_FILE = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "player_extra_fields.json"
ADVANCED_FILE = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "player_advanced_fields.json"
APPEARANCE_FILE = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "player_appearance_fields.json"
SIGNATURE_OPTIONS_FILE = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "player_signature_options.json"
PLAYBOOK_PLAYS_FILE = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "playbook_plays.json"
BACKUP_DIR = Path.home() / "Documents" / "NBA2K27_PlayerEditor_Backups"
GAME_EXPECTED = os.path.normcase(os.path.normpath(GAME_PATH))

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
user32.FindWindowW.argtypes = [ct.c_wchar_p, ct.c_wchar_p]
user32.FindWindowW.restype = ct.c_void_p
user32.SetForegroundWindow.argtypes = [ct.c_void_p]
user32.SetForegroundWindow.restype = ct.c_int


class MEMORY_BASIC_INFORMATION(ct.Structure):
    _fields_ = [
        ("BaseAddress", ct.c_void_p), ("AllocationBase", ct.c_void_p),
        ("AllocationProtect", ct.c_uint32), ("PartitionId", ct.c_uint16),
        ("RegionSize", ct.c_size_t), ("State", ct.c_uint32),
        ("Protect", ct.c_uint32), ("Type", ct.c_uint32),
    ]


def find_game() -> psutil.Process:
    for proc in psutil.process_iter(["name", "exe"]):
        try:
            if proc.info.get("name", "").lower() == "nba2k27.exe":
                path = proc.info.get("exe")
                if path and os.path.normcase(os.path.normpath(path)) == GAME_EXPECTED:
                    return proc
        except (psutil.NoSuchProcess, psutil.AccessDenied, TypeError):
            continue
    raise RuntimeError("没有找到 F:\\game\\NBA2K27\\NBA2K27.exe，请先启动游戏。")


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
    return min(99, max(25, round(25 + raw * 85 / 255)))


def rating_to_raw(rating: int) -> int:
    return max(0, min(255, round((rating - 25) * 255 / 85)))


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


async def _recognize_png(data: bytes) -> str:
    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream)
    writer.write_bytes(data)
    await writer.store_async()
    writer.detach_stream()
    stream.seek(0)
    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()
    engine = OcrEngine.try_create_from_user_profile_languages()
    return (await engine.recognize_async(bitmap)).text if engine else ""


def screen_player_text() -> str:
    image = ImageGrab.grab()
    width, height = image.size
    image = image.crop((round(width * 0.34), 0, width, round(height * 0.45)))
    output = BytesIO()
    image.save(output, format="PNG")
    return asyncio.run(_recognize_png(output.getvalue()))


class GameMemory:
    def __init__(self):
        self.process = find_game()
        self.pid = self.process.pid
        self.handle = k32.OpenProcess(PROCESS_VM_READ | PROCESS_VM_WRITE | PROCESS_VM_OPERATION | PROCESS_QUERY_INFORMATION, False, self.pid)
        if not self.handle:
            raise OSError(ct.get_last_error(), "无法打开游戏进程")
        self.base = module_base(self.pid)
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
            raise RuntimeError("这版游戏的球员表位置与已验证版本不符，已停止读取。")
        self.players = self._load_players()
        if len(self.players) < 100:
            self.close()
            raise RuntimeError("球员表验证失败，已停止读取。")

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
        team_details: dict[int, tuple[str, str]] = {}
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
                        team_details[team_ptr] = (team_name, classify_league(team_name, roster_type))
                    except OSError:
                        team_details[team_ptr] = ("球队未知", "其他联赛")
                stat_id = struct.unpack_from("<H", row, 304)[0]
                overall = None
                if stats and stat_id < self.stats_count:
                    overall = (struct.unpack_from("<I", stats, stat_id * 64 + 60)[0] >> 21) & 0x7F
                    if not 25 <= overall <= 99:
                        overall = None
                team_name, league = team_details.get(team_ptr, ("自由球员", "自由球员"))
                result.append({"index": index, "name": f"{first} {last}",
                               "uid": struct.unpack_from("<H", row, 296)[0],
                               "address": self.table + index * PLAYER_STRIDE,
                               "team": team_name, "league": league, "overall": overall})
        return result

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

    def edit_playbook_slots(self, book: dict, updates: dict[int, int]) -> Path:
        """Validate and save one or more playbook slots as a single undoable edit."""
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
            if crc and crc not in self.play_crc_pool:
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
        if not candidates and deep:
            candidates = scan(regions)
        if not candidates:
            return None
        # Game keeps older editor panel copies in the same heap. The active
        # panel is the latest one seen in the current inspected build.
        _panel, player, record = max(candidates, key=lambda item: item[0])
        return player, record

    def find_current_on_screen(self) -> dict | None:
        text = " ".join(screen_player_text().upper().split())
        compact = re.sub(r"\s+", "", text)
        draft = re.search(r"选秀.{0,20}?[（(](\d{4})[）)]", compact)
        years = re.search(r"职业年限[：:](\d{1,2})", compact)
        matches = []
        for player in self.players:
            name = " ".join(player["name"].upper().split())
            if name and name in text:
                matches.append(player)
        if draft:
            target = int(draft.group(1))
            matches = [player for player in matches
                       if 1900 + ((self.u32(player["address"] + 440) >> 8) & 0xFF) == target]
        if years:
            target = int(years.group(1))
            matches = [player for player in matches
                       if ((self.u32(player["address"] + 992) >> 17) & 31) == target]
        if len(matches) == 1:
            return matches[0]
        league_matches = [player for player in matches if player["index"] < 5000]
        return league_matches[0] if len(league_matches) == 1 else None

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
    """Keep OCR and the broad memory scan outside the Tk process."""
    memory = None
    try:
        memory = GameMemory()
        if deep:
            found = memory.find_current_editor(deep=True)
        else:
            player = memory.find_current_on_screen()
            editor = memory.find_current_editor(deep=False)
            found = (editor if editor and editor[0]["uid"] == player["uid"]
                     else (player, None)) if player else editor
        if found:
            player, address = found
            sender.send({"pid": memory.pid, "index": player["index"],
                         "uid": player["uid"], "address": address})
        else:
            sender.send({"pid": memory.pid})
    except Exception as exc:
        sender.send({"error": str(exc)})
    finally:
        if memory:
            memory.close()
        sender.close()


class PlayerEditor(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("NBA 2K27 球员与战术修改器")
        self.geometry("1260x800")
        self.minsize(1000, 650)
        self.fields = json.loads(FIELD_FILE.read_text(encoding="utf-8"))
        self.extra_fields = json.loads(EXTRA_FILE.read_text(encoding="utf-8"))
        self.advanced_fields = json.loads(ADVANCED_FILE.read_text(encoding="utf-8"))
        self.appearance_fields = json.loads(APPEARANCE_FILE.read_text(encoding="utf-8"))
        self.signature_options = json.loads(SIGNATURE_OPTIONS_FILE.read_text(encoding="utf-8"))
        self.play_catalog = {int(key, 16): value for key, value in
                             json.loads(PLAYBOOK_PLAYS_FILE.read_text(encoding="utf-8")).items()}
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
        self.book_option_map: dict[str, dict] = {}
        self.play_catalog_items: dict[str, int] = {}
        self.play_use_count: Counter[int] = Counter()
        self.detect_process = None
        self.detect_receiver = None
        self.detect_started = 0.0
        self.detect_memory = None
        self.height = tk.StringVar()
        self.wingspan = tk.StringVar()
        self.arm_scale = tk.StringVar()
        self.custom_scales = tk.BooleanVar()
        self.status = tk.StringVar(value="连接游戏以读取球员名单")
        self.search = tk.StringVar()
        self.league_filter = tk.StringVar(value="全部联赛")
        self.team_filter = tk.StringVar(value="全部球队")
        self.book_choice = tk.StringVar()
        self.book_search = tk.StringVar()
        self.play_type_filter = tk.StringVar(value="全部打法")
        self.play_position_filter = tk.StringVar(value="全部位置")
        self.play_search = tk.StringVar()
        self.playbook_status = tk.StringVar(value="打开此页后读取游戏战术手册")
        self._make_ui()
        self.protocol("WM_DELETE_WINDOW", self._quit)
        self.after(200, self.connect)

    def _make_ui(self):
        warning = tk.Frame(self, bg="#B00020", padx=14, pady=10)
        warning.pack(fill="x")
        tk.Label(warning, text="⚠  必须离线运行游戏  ⚠", bg="#B00020", fg="white",
                 font=("Microsoft YaHei UI", 21, "bold")).pack()
        tk.Label(warning, text="仅用于本地离线游戏。启动游戏前请断开网络。",
                 bg="#B00020", fg="white", font=("Microsoft YaHei UI", 11)).pack(pady=(3, 0))
        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")
        ttk.Button(top, text="重新连接", command=self.connect).pack(side="left")
        ttk.Button(top, text="识别游戏当前球员", command=self.detect_current).pack(side="left", padx=8)
        ttk.Button(top, text="深度识别", command=lambda: self.detect_current(deep=True)).pack(side="left")
        ttk.Label(top, textvariable=self.status).pack(side="left", padx=12)
        middle = ttk.PanedWindow(self, orient="horizontal")
        middle.pack(fill="both", expand=True, padx=10, pady=6)
        left = ttk.Frame(middle, padding=4)
        middle.add(left, weight=2)
        self.main_panes = middle
        self.player_panel = left
        ttk.Label(left, text="查找球员").pack(anchor="w")
        self.league_box = ttk.Combobox(left, textvariable=self.league_filter, state="readonly",
                                       values=("全部联赛", *LEAGUE_ORDER))
        self.league_box.pack(fill="x", pady=(4, 4))
        self.league_box.bind("<<ComboboxSelected>>", lambda _event: self._filter())
        self.team_box = ttk.Combobox(left, textvariable=self.team_filter, state="readonly", values=("全部球队",))
        self.team_box.pack(fill="x", pady=(0, 4))
        self.team_box.bind("<<ComboboxSelected>>", lambda _event: self._filter())
        entry = ttk.Entry(left, textvariable=self.search)
        entry.pack(fill="x", pady=(4, 8))
        self.search.trace_add("write", lambda *_: self._filter())
        player_frame = ttk.Frame(left)
        player_frame.pack(fill="both", expand=True)
        self.player_tree = ttk.Treeview(player_frame, columns=("overall", "index"), show="tree headings",
                                        selectmode="browse")
        self.player_tree.heading("#0", text="联赛 / 球队 / 球员")
        self.player_tree.heading("overall", text="总评")
        self.player_tree.heading("index", text="编号")
        self.player_tree.column("#0", width=280, stretch=True)
        self.player_tree.column("overall", width=54, stretch=False, anchor="center")
        self.player_tree.column("index", width=56, stretch=False, anchor="center")
        vertical = ttk.Scrollbar(player_frame, orient="vertical", command=self.player_tree.yview)
        horizontal = ttk.Scrollbar(player_frame, orient="horizontal", command=self.player_tree.xview)
        self.player_tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.player_tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        player_frame.rowconfigure(0, weight=1)
        player_frame.columnconfigure(0, weight=1)
        self.player_tree.bind("<<TreeviewSelect>>", self._select)
        right = ttk.Frame(middle, padding=4)
        middle.add(right, weight=3)
        self.header = ttk.Label(right, text="请选择球员", font=("Microsoft YaHei UI", 14, "bold"))
        self.header.pack(anchor="w", pady=(0, 10))
        self.tabs = ttk.Notebook(right)
        self.tabs.pack(fill="both", expand=True)
        self._make_profile_tab()
        body = ttk.Frame(self.tabs, padding=16)
        self.tabs.add(body, text="身体")
        for i, (label, var, hint) in enumerate([
            ("身高（厘米）", self.height, "可超过游戏菜单上限；数据字段上限 327.67"),
            ("臂展（厘米）", self.wingspan, "数据字段上限 327.67；模型变化尚未验证"),
            ("手臂比例（实验）", self.arm_scale, "数值可保存；目前未观察到模型变化"),
        ]):
            ttk.Label(body, text=label).grid(row=i, column=0, sticky="w", pady=8)
            ttk.Entry(body, textvariable=var, width=16).grid(row=i, column=1, sticky="w", padx=12)
            ttk.Label(body, text=hint, foreground="#666666").grid(row=i, column=2, sticky="w")
        ratio_actions = ttk.Frame(body)
        ratio_actions.grid(row=3, column=0, columnspan=3, sticky="w", pady=(5, 10))
        ttk.Button(ratio_actions, text="按身高 × 1.4 填臂展",
                   command=lambda: self._fill_body_ratio(from_height=True)).pack(side="left")
        ttk.Button(ratio_actions, text="按臂展 ÷ 1.4 填身高",
                   command=lambda: self._fill_body_ratio(from_height=False)).pack(side="left", padx=10)
        ttk.Checkbutton(body, text="使用自定义外观比例", variable=self.custom_scales).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=10)
        ttk.Label(body, text="1.4 按钮只填数值，不自动保存；模型效果待验证。修改手臂比例会启用自定义外观比例。",
                  foreground="#666666", wraplength=650).grid(row=5, column=0, columnspan=3, sticky="w")
        self._make_signature_tab()
        self._make_playbook_tab()
        groups = []
        for field in self.fields:
            if field["group"] not in groups:
                groups.append(field["group"])
        for group in groups:
            page = ttk.Frame(self.tabs)
            self.tabs.add(page, text={"Offense": "进攻", "Defense": "防守", "Athleticism": "运动", "Durability": "耐久", "Mental": "意识", "Misc": "其他"}.get(group, group))
            canvas = tk.Canvas(page, borderwidth=0, highlightthickness=0)
            bar = ttk.Scrollbar(page, orient="vertical", command=canvas.yview)
            inner = ttk.Frame(canvas, padding=12)
            inner.bind("<Configure>", lambda _e, c=canvas: c.configure(scrollregion=c.bbox("all")))
            canvas.create_window((0, 0), window=inner, anchor="nw")
            canvas.configure(yscrollcommand=bar.set)
            canvas.pack(side="left", fill="both", expand=True)
            bar.pack(side="right", fill="y")
            for n, field in enumerate(f for f in self.fields if f["group"] == group):
                label = ttk.Label(inner, text=field["label"], width=29)
                label.grid(row=n, column=0, sticky="w", pady=4)
                var = tk.StringVar()
                self.rating_inputs[field["id"]] = var
                ttk.Entry(inner, textvariable=var, width=8).grid(row=n, column=1, sticky="w", padx=10)
            self._bind_canvas_wheel(canvas, inner, bar)
        self._make_extra_tabs()
        self._make_advanced_tab()
        bottom = ttk.Frame(self, padding=10)
        bottom.pack(fill="x")
        ttk.Button(bottom, text="保存修改", command=self.save).pack(side="right")
        ttk.Button(bottom, text="撤销上次保存", command=self.undo).pack(side="right", padx=8)
        ttk.Button(bottom, text="重新读取", command=self.reload).pack(side="right", padx=8)
        ttk.Button(bottom, text="批量修改", command=self.batch_edit).pack(side="right", padx=8)
        ttk.Button(bottom, text="复制 DNA", command=self.copy_dna_dialog).pack(side="right", padx=8)
        ttk.Label(bottom, text="修改会立即写入游戏内存；请在游戏里保存名单。", foreground="#666666").pack(side="left")
        self.tabs.bind("<<NotebookTabChanged>>", self._tab_changed)

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
            target = (source * BODY_RATIO if from_height else source / BODY_RATIO).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP)
            if not BODY_MIN_CM <= target <= BODY_MAX_CM:
                raise ValueError(f"按 1.4 换算得到的{target_name}为 {target} 厘米，"
                                 f"超出可保存范围 {BODY_MIN_CM}–{BODY_MAX_CM} 厘米。")
            target_var.set(f"{target:.2f}")
            self.status.set(f"已按 1.4 填入{target_name} {target:.2f} 厘米；请点击“保存修改”。")
        except ValueError as exc:
            messagebox.showerror("比例换算失败", str(exc), parent=self)

    def _make_profile_tab(self):
        page = ttk.Frame(self.tabs)
        self.tabs.add(page, text="个人资料")
        canvas = tk.Canvas(page, borderwidth=0, highlightthickness=0)
        bar = ttk.Scrollbar(page, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas, padding=14)
        inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=bar.set)
        canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        ttk.Label(inner, text="修改姓名后，游戏可能需要重新打开球员页面才能刷新显示。",
                  foreground="#666666").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))
        for row, (key, label) in enumerate(PROFILE_UI, 1):
            ttk.Label(inner, text=label, width=18).grid(row=row, column=0, sticky="w", pady=5)
            var = tk.StringVar()
            self.profile_inputs[key] = var
            options = POSITIONS if key in ("position", "secondary_position") else (
                HANDS if key == "dominant_hand" else DUNK_HANDS if key == "dunk_hand" else None)
            if options:
                ttk.Combobox(inner, textvariable=var, values=options, state="readonly", width=26).grid(
                    row=row, column=1, sticky="w", padx=10)
            else:
                ttk.Entry(inner, textvariable=var, width=28).grid(row=row, column=1, sticky="w", padx=10)
        self._bind_canvas_wheel(canvas, inner, bar)

    def _make_signature_tab(self):
        outer = ttk.Frame(self.tabs, padding=4)
        self.tabs.add(outer, text="动作")
        ttk.Label(outer, text="从列表选动作名称，也可输入编号；未收录名称的项目保留数字输入。修改后点“保存修改”。",
                  foreground="#666666").pack(anchor="w", padx=8, pady=5)
        book = ttk.Notebook(outer)
        book.pack(fill="both", expand=True)
        groups = {
            "Jump Shooting": "投篮", "Jump Shooting II": "花式投篮",
            "Layups And Dunks": "上篮扣篮", "Post Game": "背身",
            "Ball Handling": "控球", "Misc": "其他",
        }
        for group, title in groups.items():
            page = ttk.Frame(book)
            book.add(page, text=title)
            canvas = tk.Canvas(page, borderwidth=0, highlightthickness=0)
            bar = ttk.Scrollbar(page, orient="vertical", command=canvas.yview)
            inner = ttk.Frame(canvas, padding=12)
            inner.bind("<Configure>", lambda _event, c=canvas: c.configure(scrollregion=c.bbox("all")))
            canvas.create_window((0, 0), window=inner, anchor="nw")
            canvas.configure(yscrollcommand=bar.set)
            canvas.pack(side="left", fill="both", expand=True)
            bar.pack(side="right", fill="y")
            fields = [field for field in self.signature_fields if field["group"] == group]
            for row, field in enumerate(fields):
                ttk.Label(inner, text=signature_label(field), width=25).grid(row=row, column=0, sticky="w", pady=4)
                var = tk.StringVar()
                self.signature_inputs[field["id"]] = var
                options = self.signature_options.get(field["id"])
                if options:
                    labels = [f"{index} · {signature_option_name(name)}" for index, name in enumerate(options)]
                    ttk.Combobox(inner, textvariable=var, values=labels, width=36).grid(
                        row=row, column=1, sticky="w", padx=10)
                else:
                    ttk.Entry(inner, textvariable=var, width=16).grid(row=row, column=1, sticky="w", padx=10)
                hint = f"{len(options)} 个已收录名称" if options else f"编号 0–{(1 << field['bits']) - 1}"
                ttk.Label(inner, text=hint, foreground="#666666").grid(
                    row=row, column=2, sticky="w")
            self._bind_canvas_wheel(canvas, inner, bar)

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

        note = ("左侧选择球队手册的槽位，右侧从已载入战术中挑选；按住 Ctrl/Shift 可多选并批量添加或删除。"
                "打法部分来自游戏记录，其余按名称归类；位置为关联位置。")
        ttk.Label(page, text=note, foreground="#666666", wraplength=1050).pack(anchor="w", pady=(7, 4))
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
        self.play_slot_tree.heading("type", text="打法")
        self.play_slot_tree.heading("position", text="关联位置")
        self.play_slot_tree.column("#0", width=285, stretch=True)
        self.play_slot_tree.column("type", width=80, stretch=False)
        self.play_slot_tree.column("position", width=90, stretch=False)
        slot_scroll = ttk.Scrollbar(slot_frame, orient="vertical", command=self.play_slot_tree.yview)
        self.play_slot_tree.configure(yscrollcommand=slot_scroll.set)
        self.play_slot_tree.pack(side="left", fill="both", expand=True)
        slot_scroll.pack(side="right", fill="y")
        slot_actions = ttk.Frame(left)
        slot_actions.pack(fill="x")
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
                                           values=("全部打法", *TYPE_ORDER), width=11)
        self.play_type_box.pack(side="left", padx=(0, 4))
        self.play_position_box = ttk.Combobox(filters, textvariable=self.play_position_filter,
                                               state="readonly", values=("全部位置", *POSITION_NAMES, "未标注"), width=12)
        self.play_position_box.pack(side="left", padx=4)
        ttk.Entry(filters, textvariable=self.play_search, width=24).pack(side="left", fill="x", expand=True, padx=4)
        self.play_type_box.bind("<<ComboboxSelected>>", lambda _event: self._filter_play_catalog())
        self.play_position_box.bind("<<ComboboxSelected>>", lambda _event: self._filter_play_catalog())
        self.play_search.trace_add("write", lambda *_: self._filter_play_catalog())
        ttk.Label(right, text="可选战术（按打法分组，可搜索英文名或 CRC）").pack(anchor="w", pady=(7, 4))
        catalog_frame = ttk.Frame(right)
        catalog_frame.pack(fill="both", expand=True)
        self.play_catalog_tree = ttk.Treeview(catalog_frame, columns=("detail", "position", "books"),
                                              show="tree headings", selectmode="extended")
        self.play_catalog_tree.heading("#0", text="打法 / 战术名称")
        self.play_catalog_tree.heading("detail", text="细分")
        self.play_catalog_tree.heading("position", text="关联位置")
        self.play_catalog_tree.heading("books", text="使用次数")
        self.play_catalog_tree.column("#0", width=310, stretch=True)
        self.play_catalog_tree.column("detail", width=90, stretch=False)
        self.play_catalog_tree.column("position", width=95, stretch=False)
        self.play_catalog_tree.column("books", width=65, stretch=False, anchor="center")
        catalog_scroll = ttk.Scrollbar(catalog_frame, orient="vertical", command=self.play_catalog_tree.yview)
        self.play_catalog_tree.configure(yscrollcommand=catalog_scroll.set)
        self.play_catalog_tree.pack(side="left", fill="both", expand=True)
        catalog_scroll.pack(side="right", fill="y")
        ttk.Button(right, text="批量添加所选战术到空槽", command=self._batch_add_playbook).pack(
            anchor="e", pady=(5, 0))
        footer = ttk.Frame(page)
        footer.pack(fill="x", pady=(6, 0))
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

    @staticmethod
    def _bind_canvas_wheel(canvas: tk.Canvas, *widgets: tk.Widget):
        """Scroll a tab even while the pointer is over a label or input field."""
        def on_wheel(event):
            if event.delta:
                units = -int(event.delta / 120)
                if not units:
                    units = -1 if event.delta > 0 else 1
                canvas.yview_scroll(units * 3, "units")
            return "break"

        def bind_tree(widget):
            widget.bind("<MouseWheel>", on_wheel, add="+")
            for child in widget.winfo_children():
                bind_tree(child)

        bind_tree(canvas)
        for widget in widgets:
            bind_tree(widget)

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
        scopes["能力 · 全部"] = ("ratings", self.fields, 25, 99)
        for title, group in rating_groups.items():
            scopes[f"能力 · {title}"] = ("ratings", [f for f in self.fields if f["group"] == group], 25, 99)
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
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=f"当前球员：{self.selected['name']}").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 14))
        ttk.Label(frame, text="修改范围").grid(row=1, column=0, sticky="w", pady=6)
        scope = tk.StringVar(value="能力 · 全部")
        ttk.Combobox(frame, textvariable=scope, values=list(scopes), state="readonly", width=26).grid(row=1, column=1, sticky="w")
        value_hint = tk.StringVar(value="目标数值（25–99）")
        ttk.Label(frame, textvariable=value_hint).grid(row=2, column=0, sticky="w", pady=6)
        value = tk.StringVar(value="99")
        entry = ttk.Entry(frame, textvariable=value, width=20)
        entry.grid(row=2, column=1, sticky="w")
        ttk.Label(frame, text="会直接保存到游戏；可用主界面的“撤销上次保存”恢复。", foreground="#666666").grid(row=3, column=0, columnspan=2, sticky="w", pady=(10, 16))

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
        ttk.Button(frame, text="保存修改", command=apply_batch).grid(row=4, column=1, sticky="e")
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
        dialog.geometry("680x660")
        dialog.transient(self)
        frame = ttk.Frame(dialog, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=f"来源：{source['name']} · {source['team']} · #{source['index']}",
                  font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w", pady=(0, 10))
        ttk.Label(frame, text="数据 DNA：能力、倾向、徽章、动作；外貌 DNA：身材、面部参数、头发与穿戴。",
                  foreground="#666666").pack(anchor="w")
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
        listbox = tk.Listbox(frame, exportselection=False)
        listbox.pack(fill="both", expand=True)
        candidates = []

        def refresh(*_args):
            candidates.clear()
            listbox.delete(0, "end")
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
                candidates.append(player)
                rating = player["overall"] if player["overall"] is not None else "—"
                listbox.insert("end", f"{player['name']} · {player['team']} · 总评 {rating} · #{player['index']}")

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
        ttk.Label(frame, text="整队复制会跳过来源球员；完成后可用主窗口的“撤销上次保存”恢复。",
                  foreground="#666666").pack(anchor="w", pady=(8, 4))
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(8, 0))

        def apply_copy():
            try:
                if scope.get() == "team":
                    if team.get() == "全部球队":
                        raise ValueError("整队复制请先选择一支目标球队")
                    targets = [player for player in self.memory.players
                               if player["team"] == team.get() and player["index"] != source["index"]]
                else:
                    chosen = listbox.curselection()
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
        ttk.Button(buttons, text="复制到目标", command=apply_copy).pack(side="right", padx=8)
        dialog.grab_set()

    def _has_pending_edits(self) -> bool:
        if any(self.profile_inputs[key].get() != self._profile_display(key, self.baseline["profile"][key])
               for key, _label in PROFILE_UI):
            return True
        if (self.height.get() != str(self.baseline["height_cm"]) or
                self.wingspan.get() != str(self.baseline["wingspan_cm"]) or
                self.arm_scale.get() != str(self.baseline["arm_scale"]) or
                self.custom_scales.get() != self.baseline["custom_scales"]):
            return True
        if any(self.rating_inputs[f["id"]].get() != str(self.baseline["ratings"][f["id"]]) for f in self.fields):
            return True
        if any(self.extra_inputs[f["section"] + ":" + f["id"]].get() !=
               str(self.baseline["extras"][f["section"] + ":" + f["id"]]) for f in self.extra_fields):
            return True
        for field in self.signature_fields:
            try:
                value = self._signature_value(field, self.signature_inputs[field["id"]].get())
            except ValueError:
                return True
            if value != self.baseline["signatures"][field["id"]]:
                return True
        return False

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
        for section, title in (("Tendencies", "倾向"), ("Badges", "徽章")):
            outer = ttk.Frame(self.tabs, padding=4)
            self.tabs.add(outer, text=title)
            ttk.Label(outer, text="倾向为 0–100，热区为 0–3；徽章等级为 0–5，开关为 0/1。修改后点下方“保存修改”。",
                      foreground="#666666").pack(anchor="w", padx=8, pady=5)
            book = ttk.Notebook(outer)
            book.pack(fill="both", expand=True)
            groups = list(dict.fromkeys(field["group"] for field in self.extra_fields if field["section"] == section))
            for group in groups:
                page = ttk.Frame(book)
                book.add(page, text=group_names.get(group, group))
                canvas = tk.Canvas(page, borderwidth=0, highlightthickness=0)
                bar = ttk.Scrollbar(page, orient="vertical", command=canvas.yview)
                inner = ttk.Frame(canvas, padding=12)
                inner.bind("<Configure>", lambda _e, c=canvas: c.configure(scrollregion=c.bbox("all")))
                canvas.create_window((0, 0), window=inner, anchor="nw")
                canvas.configure(yscrollcommand=bar.set)
                canvas.pack(side="left", fill="both", expand=True)
                bar.pack(side="right", fill="y")
                items = [field for field in self.extra_fields if field["section"] == section and field["group"] == group]
                for n, field in enumerate(items):
                    key = section + ":" + field["id"]
                    ttk.Label(inner, text=field["label"], width=32).grid(row=n, column=0, sticky="w", pady=4)
                    var = tk.StringVar()
                    self.extra_inputs[key] = var
                    ttk.Entry(inner, textvariable=var, width=8).grid(row=n, column=1, sticky="w", padx=10)
                self._bind_canvas_wheel(canvas, inner, bar)

    def _make_advanced_tab(self):
        page = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(page, text="高级字段")
        ttk.Label(page, text="搜索字段后选择一项，修改其原始数值。字段按游戏数据格式显示。",
                  foreground="#666666").pack(anchor="w", pady=(0, 6))
        self.advanced_search = tk.StringVar()
        ttk.Entry(page, textvariable=self.advanced_search).pack(fill="x", pady=(0, 6))
        self.advanced_search.trace_add("write", lambda *_: self._advanced_refresh())
        frame = ttk.Frame(page)
        frame.pack(fill="both", expand=True)
        self.advanced_tree = ttk.Treeview(frame, columns=("section", "group", "field", "value"), show="headings")
        for key, label, width in (("section", "类别", 90), ("group", "分组", 110),
                                  ("field", "字段", 220), ("value", "当前原始值", 110)):
            self.advanced_tree.heading(key, text=label)
            self.advanced_tree.column(key, width=width, anchor="w")
        bar = ttk.Scrollbar(frame, orient="vertical", command=self.advanced_tree.yview)
        self.advanced_tree.configure(yscrollcommand=bar.set)
        self.advanced_tree.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self.advanced_tree.bind("<<TreeviewSelect>>", self._advanced_select)
        controls = ttk.Frame(page)
        controls.pack(fill="x", pady=(8, 0))
        self.advanced_info = tk.StringVar(value="请选择字段")
        ttk.Label(controls, textvariable=self.advanced_info).pack(side="left", padx=(0, 10))
        self.advanced_value = tk.StringVar()
        ttk.Entry(controls, textvariable=self.advanced_value, width=16).pack(side="left")
        ttk.Button(controls, text="保存此字段", command=self._advanced_save).pack(side="left", padx=8)

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
            haystack = f"{field['section']} {field['group']} {field['id']} {field['label']}".casefold()
            if term and term not in haystack:
                continue
            self.advanced_tree.insert("", "end", iid=str(index), values=(
                section_names.get(field["section"], field["section"]), field["group"], field["label"],
                self._advanced_raw(row, field)))

    def _advanced_select(self, _event=None):
        selected = self.advanced_tree.selection()
        if not selected:
            return
        field = self.advanced_fields[int(selected[0])]
        value = self._advanced_raw(self.memory.read(self.selected["address"], PLAYER_STRIDE), field)
        self.advanced_value.set(str(value))
        range_text = "浮点数" if field["kind"] == "float" else f"0–{(1 << field['bits']) - 1}"
        self.advanced_info.set(f"{field['id']}  {range_text}")

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
            value = int(self.extra_inputs[key].get().strip())
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

    def connect(self):
        try:
            if self.memory:
                self.memory.close()
            self.memory = GameMemory()
            self.selected = None
            self.current_edit_address = None
            self.baseline = None
            self.last_backup = None
            self.playbook_last_backup = None
            teams = sorted({player["team"] for player in self.memory.players})
            self.team_box.configure(values=("全部球队", *teams))
            if self.team_filter.get() not in teams:
                self.team_filter.set("全部球队")
            self._filter()
            self.status.set(f"已连接游戏；读取到 {len(self.memory.players)} 名球员")
            if self.tabs.select() == str(self.playbook_page):
                self._refresh_playbooks()
        except Exception as exc:
            self.memory = None
            self.status.set(str(exc))

    def _tab_changed(self, _event=None):
        playbook_open = self.tabs.select() == str(self.playbook_page)
        player_visible = str(self.player_panel) in self.main_panes.panes()
        if playbook_open and player_visible:
            self.main_panes.forget(self.player_panel)
        elif not playbook_open and not player_visible:
            self.main_panes.insert(0, self.player_panel, weight=2)
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
        return {"name": f"未知战术 0x{crc:08X}", "group": "其他", "detail": "未识别",
                "positions": [], "source": "未识别"}

    @staticmethod
    def _play_position_label(positions: list[int]) -> str:
        return " / ".join(POSITION_NAMES[index - 1] for index in positions if 1 <= index <= 5) or "未标注"

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
            detail = entry["group"] if entry else ""
            position = self._play_position_label(entry["positions"]) if entry else ""
            suffix = " · 保留" if slot >= PLAYBOOK_EDITABLE_SLOTS else ""
            tree.insert("", "end", iid=f"slot:{slot}", text=f"{slot + 1:02d}  {label}{suffix}",
                        values=(detail, position))
        if selected_id and tree.exists(selected_id):
            tree.selection_set(selected_id)
            tree.see(selected_id)
        used = sum(bool(value) for value in book["slots"][:PLAYBOOK_EDITABLE_SLOTS])
        self.playbook_status.set(f"{book['name']}：已用 {used}/80 槽；右侧可选 {len(self.memory.play_crc_pool)} 种战术")

    def _filter_play_catalog(self):
        if not hasattr(self, "play_catalog_tree"):
            return
        tree = self.play_catalog_tree
        selected = tree.selection()
        selected_id = selected[0] if selected else None
        tree.delete(*tree.get_children(""))
        self.play_catalog_items = {}
        if not self.memory or not getattr(self.memory, "play_crc_pool", None):
            return
        group_filter = self.play_type_filter.get()
        position_filter = self.play_position_filter.get()
        term = self.play_search.get().strip().casefold()
        grouped: dict[str, list[tuple[int, dict]]] = {}
        for crc in self.memory.play_crc_pool:
            entry = self._play_meta(crc)
            if group_filter != "全部打法" and entry["group"] != group_filter:
                continue
            positions = entry["positions"]
            if position_filter == "未标注" and positions:
                continue
            if position_filter in POSITION_NAMES and POSITION_NAMES.index(position_filter) + 1 not in positions:
                continue
            if term and term not in entry["name"].casefold() and term not in f"{crc:08X}".casefold():
                continue
            grouped.setdefault(entry["group"], []).append((crc, entry))
        for group in TYPE_ORDER:
            plays = grouped.get(group, [])
            if not plays:
                continue
            group_id = f"play-group:{group}"
            tree.insert("", "end", iid=group_id, text=f"{group}（{len(plays)}）", open=bool(term))
            for crc, entry in sorted(plays, key=lambda item: (item[1]["name"], item[0])):
                item_id = f"play:{crc:08X}"
                tree.insert(group_id, "end", iid=item_id, text=entry["name"],
                            values=(entry["detail"], self._play_position_label(entry["positions"]),
                                    self.play_use_count.get(crc, 0)))
                self.play_catalog_items[item_id] = crc
        if selected_id and tree.exists(selected_id):
            tree.selection_set(selected_id)
            tree.see(selected_id)

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
            crcs = [self.play_catalog_items[item]
                    for group in self.play_catalog_tree.get_children("")
                    for item in self.play_catalog_tree.get_children(group)
                    if item in selected and item in self.play_catalog_items]
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

    def _filter(self):
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
        for player in self.memory.players:
            if league_choice != "全部联赛" and player["league"] != league_choice:
                continue
            if team_choice != "全部球队" and player["team"] != team_choice:
                continue
            if term and term not in player["name"].casefold() and term != str(player["index"]):
                continue
            grouped.setdefault(player["league"], {}).setdefault(player["team"], []).append(player)

        selected_id = None
        for league in LEAGUE_ORDER:
            team_groups = grouped.get(league, {})
            if not team_groups and not (not term and league == "WNBA" and
                                         league_choice in ("全部联赛", "WNBA")):
                continue
            count = sum(len(players) for players in team_groups.values())
            league_id = f"league:{league}"
            tree.insert("", "end", iid=league_id, text=f"{league}（{count}）",
                        open=term != "" or self._tree_open_state.get(league_id, league == "NBA"))
            for team in sorted(team_groups):
                players = team_groups[team]
                team_id = f"team:{league}:{team}"
                tree.insert(league_id, "end", iid=team_id, text=f"{team}（{len(players)}）",
                            open=term != "" or self._tree_open_state.get(team_id, False))
                for player in sorted(players, key=lambda item: item["index"]):
                    player_id = f"player:{player['index']}"
                    rating = player["overall"] if player["overall"] is not None else "—"
                    tree.insert(team_id, "end", iid=player_id, text=player["name"],
                                values=(rating, player["index"]))
                    self.player_items[player_id] = player
                    if self.selected and player["index"] == self.selected["index"] and player["uid"] == self.selected["uid"]:
                        selected_id = player_id
        if selected_id:
            parent = tree.parent(selected_id)
            tree.item(parent, open=True)
            tree.item(tree.parent(parent), open=True)
            tree.selection_set(selected_id)
            tree.focus(selected_id)
            tree.see(selected_id)

    def _select(self, _event=None):
        selected = self.player_tree.selection()
        if not selected:
            return
        player = self.player_items.get(selected[0])
        if not player:
            return
        if self.selected != player:
            self.selected = player
            self.current_edit_address = None
            self.reload()

    def detect_current(self, *, deep: bool = False):
        if self.detect_process is not None:
            self.status.set("正在识别，请稍候…")
            return
        if not self.memory:
            self.connect()
        if not self.memory:
            return
        self.detect_memory = self.memory
        self.status.set("正在识别当前球员…" if not deep else "正在全面扫描，可能需要约一分钟…")
        if deep:
            self._start_detect(deep)
        else:
            self.withdraw()
            game_window = user32.FindWindowW(None, "NBA 2K27")
            if game_window:
                user32.SetForegroundWindow(game_window)
            self.after(250, lambda: self._start_detect(deep))

    def _start_detect(self, deep: bool):
        try:
            receiver, sender = mp.get_context("spawn").Pipe(duplex=False)
            process = mp.get_context("spawn").Process(
                target=detect_current_worker, args=(sender, deep), daemon=True)
            process.start()
            sender.close()
            self.detect_process = process
            self.detect_receiver = receiver
            self.detect_started = time.monotonic()
            self.after(100, lambda: self._poll_detect(deep))
        except Exception as exc:
            self.deiconify()
            self.status.set(f"无法启动识别：{exc}")

    def _poll_detect(self, deep: bool):
        process = self.detect_process
        receiver = self.detect_receiver
        if process is None or receiver is None:
            return
        result = None
        if receiver.poll():
            try:
                result = receiver.recv()
            except EOFError:
                result = {"error": "识别进程意外退出"}
        elif process.exitcode is not None:
            result = {"error": f"识别进程意外退出（代码 {process.exitcode}）"}
        elif time.monotonic() - self.detect_started > (120 if deep else 20):
            result = {"error": "识别超时，请从左侧名单选择球员"}
        if result is None:
            self.after(100, lambda: self._poll_detect(deep))
            return
        self._stop_detect()
        memory = self.detect_memory
        if not memory:
            self.deiconify()
            return
        if "error" in result:
            self._detected(memory, None, result["error"])
            return
        if result["pid"] != memory.pid:
            self._detected(memory, None, "游戏已重启，请重新连接")
            return
        player = next((p for p in memory.players if p["index"] == result.get("index")
                       and p["uid"] == result.get("uid")), None)
        found = (player, result.get("address")) if player else None
        self._detected(memory, found, None)

    def _stop_detect(self):
        if self.detect_process is not None:
            if self.detect_process.is_alive():
                self.detect_process.terminate()
            self.detect_process.join(timeout=0.2)
            self.detect_process = None
        if self.detect_receiver is not None:
            self.detect_receiver.close()
            self.detect_receiver = None

    def _detected(self, memory: GameMemory, found: tuple[dict, int] | None, error: str | None):
        self.deiconify()
        self.lift()
        if self.memory is not memory:
            return
        if error:
            self.status.set(error)
            return
        if not found:
            self.status.set("没有找到正在打开的球员编辑器；可从左侧名单选择。")
            return
        player, address = found
        self.selected = player
        self.current_edit_address = address
        self.league_filter.set(player["league"])
        self.team_filter.set(player["team"])
        self.search.set("")
        self._filter()
        self.reload()
        source = "编辑器内存" if address else "屏幕姓名"
        self.status.set(f"已从{source}识别：{player['name']}（保存前请核对姓名）")

    def reload(self):
        if not self.memory or not self.selected:
            return
        try:
            snap = self.memory.player_snapshot(self.selected, self.fields, record_address=self.current_edit_address,
                                               extra_fields=self.extra_fields,
                                               signature_fields=self.signature_fields)
            self.baseline = snap
            rating = self.selected["overall"] if self.selected["overall"] is not None else "—"
            self.header.config(text=f"{self.selected['name']}  ·  {self.selected['team']}  ·  总评 {rating}  ·  #{self.selected['index']}")
            self.height.set(str(snap["height_cm"]))
            self.wingspan.set(str(snap["wingspan_cm"]))
            self.arm_scale.set(str(snap["arm_scale"]))
            self.custom_scales.set(snap["custom_scales"])
            for key, _label in PROFILE_UI:
                self.profile_inputs[key].set(self._profile_display(key, snap["profile"][key]))
            for field in self.fields:
                self.rating_inputs[field["id"]].set(str(snap["ratings"][field["id"]]))
            for key, value in snap["extras"].items():
                self.extra_inputs[key].set(str(value))
            for key, value in snap["signatures"].items():
                field = next(field for field in self.signature_fields if field["id"] == key)
                self.signature_inputs[key].set(self._signature_display(field, value))
            self._advanced_refresh()
            self.status.set("已读取球员数据")
        except Exception as exc:
            self.status.set(str(exc))

    def save(self):
        if not self.memory or not self.selected or not self.baseline:
            return
        try:
            changes: dict[int, bytes] = {}
            for field in self.fields:
                value = int(self.rating_inputs[field["id"]].get().strip())
                if not 25 <= value <= 99:
                    raise ValueError(f"{field['label']} 须在 25 到 99 之间")
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
            height_cm = float(self.height.get().strip())
            wingspan_cm = float(self.wingspan.get().strip())
            arm_scale = float(self.arm_scale.get().strip())
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
                return
            backup = self.memory.apply(self.selected, changes, body, edit_record=self.current_edit_address, masks=masks)
            self.last_backup = backup
            self._refresh_selected_record()
            self.status.set(f"已修改 {len(changes) + len(body)} 项；备份：{backup.name}")
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc), parent=self)

    def undo(self):
        if not self.memory or not self.last_backup:
            self.status.set("没有可撤销的本次修改")
            return
        try:
            self.memory.undo(self.last_backup)
            self.last_backup = None
            self._refresh_selected_record()
            self.status.set("已撤销上次保存")
        except Exception as exc:
            messagebox.showerror("撤销失败", str(exc), parent=self)

    def _quit(self):
        self._stop_detect()
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
        PlayerEditor().mainloop()
