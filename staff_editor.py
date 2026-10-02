"""Staff record editing and the Chinese employee panel.

The panel submits field values to GameMemory, which validates the live record
before combining changes and using the existing transactional backup writer.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tkinter as tk
from tkinter import messagebox, ttk

import ui_theme as theme
from ui_theme import P, ScrollFrame, SearchBox


STAFF_SIZE = 456
STAFF_COUNT_OFFSET = 384
STAFF_TABLE_OFFSET = 392
STAFF_UID_OFFSET = 288
STAFF_TEAM_OFFSET = 24
STAFF_FIRST_OFFSET = 80
STAFF_LAST_OFFSET = 120
UNSIGNED = "未签约员工"
CATALOG_FILE = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "staff_fields.json"
SECTION_ORDER = ("能力属性", "员工徽章", "阵容熟练度", "体系风格", "执教倾向", "合同与任职", "资料与偏好", "内部标识")
JOBS = ("主教练", "首席球探", "队医", "总经理", "首席财务官", "老板", "首席助理教练", "助理总经理",
        "投篮教练", "后卫教练", "侧翼教练", "内线教练", "低位防守教练", "外线防守教练",
        "国内球探一", "国内球探二", "国内球探三", "国内球探四", "国际球探一", "国际球探二",
        "力量训练师", "体能训练师", "运动心理师", "运动科学师", "理疗师", "睡眠医生",
        "管理助理一", "管理助理二", "管理助理三", "管理助理四", "管理助理五", "管理助理六")


def load_fields() -> list[dict]:
    fields = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
    seen = set()
    for field in fields:
        bits, shift, offset = field["bits"], field["shift"], field["offset"]
        if field["id"] in seen or not (1 <= bits <= 32 and 0 <= shift < 32 and bits + shift <= 32):
            raise ValueError("员工字段定义无效")
        if not 0 <= offset <= STAFF_SIZE - (bits + shift + 7) // 8:
            raise ValueError("员工字段超出记录范围")
        seen.add(field["id"])
    return fields


def field_raw(row: bytes, field: dict) -> int:
    size = (field["bits"] + field["shift"] + 7) // 8
    word = int.from_bytes(row[field["offset"]:field["offset"] + size], "little")
    return (word >> field["shift"]) & ((1 << field["bits"]) - 1)


def field_display(field: dict, value: int) -> str:
    options = field.get("options", ())
    return f"{value} · {options[value]}" if 0 <= value < len(options) else str(value)


def parse_field(field: dict, text: str) -> int:
    text = text.split("·", 1)[0].strip()
    try:
        value = int(text, 0)
    except ValueError:
        try:
            value = int(text, 10)
        except ValueError:
            raise ValueError(f"{field['label']}：请输入整数") from None
    if not field["min"] <= value <= field["max"]:
        raise ValueError(f"{field['label']}：可填范围为 {field['min']}–{field['max']}")
    return value


def name_bytes(value: str) -> bytes:
    value = value.strip()
    if not value or any(ord(char) < 32 for char in value):
        raise ValueError("员工姓名不能为空或包含控制字符")
    encoded = value.encode("utf-16-le")
    if len(encoded) > 38:
        raise ValueError("员工姓名每部分最多 19 个普通字符")
    return encoded.ljust(40, b"\x00")


def build_changes(row: bytes, fields: list[dict], values: dict[str, int],
                  names: dict[str, str] | None = None) -> dict[int, bytes]:
    """Merge edits at byte level, including fields that share or cross bytes."""
    if len(row) != STAFF_SIZE:
        raise ValueError("员工记录长度错误")
    definitions = {field["id"]: field for field in fields}
    changed = bytearray(row)
    for key, value in values.items():
        field = definitions.get(key)
        if field is None or field.get("readonly"):
            raise ValueError("不能修改员工内部标识")
        if not isinstance(value, int) or not field["min"] <= value <= field["max"]:
            raise ValueError(f"{field['label']}：可填范围为 {field['min']}–{field['max']}")
        offset = field["offset"]
        length = (field["bits"] + field["shift"] + 7) // 8
        word = int.from_bytes(changed[offset:offset + length], "little")
        mask = ((1 << field["bits"]) - 1) << field["shift"]
        changed[offset:offset + length] = ((word & ~mask) | (value << field["shift"])).to_bytes(length, "little")
    for key, value in (names or {}).items():
        if key not in ("first_name", "last_name"):
            raise ValueError("无效的员工姓名字段")
        offset = STAFF_FIRST_OFFSET if key == "first_name" else STAFF_LAST_OFFSET
        changed[offset:offset + 40] = name_bytes(value)
    # Non-overlapping runs ensure later writes never restore another field's bits.
    result, index = {}, 0
    while index < STAFF_SIZE:
        if row[index] == changed[index]:
            index += 1
            continue
        start = index
        while index < STAFF_SIZE and row[index] != changed[index]:
            index += 1
        result[start] = bytes(changed[start:index])
    return result


class StaffPanel(ttk.Frame):
    def __init__(self, notebook: ttk.Notebook, app):
        super().__init__(notebook, padding=12)
        self.app = app
        self.fields = load_fields()
        self.field_map = {field["id"]: field for field in self.fields}
        self.people: list[dict] = []
        self.items: dict[str, dict] = {}
        self.current: dict | None = None
        self.baseline: bytes | None = None
        self.last_backup: Path | None = None
        self.loading = False
        self.team = tk.StringVar(value="全部球队")
        self.search = tk.StringVar()
        self.status = tk.StringVar(value="打开员工页后读取游戏员工")
        self.count = tk.StringVar()
        self.first_name = tk.StringVar()
        self.last_name = tk.StringVar()
        self.title = tk.StringVar(value="请选择员工")
        self.meta = tk.StringVar()
        self.inputs: dict[str, tk.StringVar] = {}
        self._make_ui()
        notebook.add(self, text="员工修改")

    @property
    def memory(self):
        return self.app.memory

    def _make_ui(self):
        top = ttk.Frame(self)
        top.pack(fill="x", pady=(0, 9))
        ttk.Label(top, text="球队 / 员工", style="Title.TLabel").pack(side="left")
        self.team_box = ttk.Combobox(top, textvariable=self.team, state="readonly", width=31)
        self.team_box.pack(side="left", padx=12)
        self.team_box.bind("<<ComboboxSelected>>", lambda _e: self._filter())
        ttk.Button(top, text="重新读取员工", command=self.reload).pack(side="right")
        ttk.Button(top, text="批量修改…", style="Accent.TButton", command=self.batch_dialog).pack(side="right", padx=8)
        split = ttk.PanedWindow(self, orient="horizontal")
        split.pack(fill="both", expand=True)
        left = ttk.Frame(split, style="Card.TFrame", padding=10, width=400)
        right = ttk.Frame(split)
        split.add(left, weight=2)
        split.add(right, weight=3)
        search_box = SearchBox(left, self.search, "搜索员工姓名、职位或球队…", icons=self.app._icon)
        search_box.pack(fill="x")
        self.search_entry = search_box.entry
        self.search.trace_add("write", lambda *_: self._filter())
        tools = ttk.Frame(left, style="Card.TFrame")
        tools.pack(fill="x", pady=6)
        ttk.Label(tools, textvariable=self.count, style="CardMuted.TLabel").pack(side="left")
        ttk.Button(tools, text="全选列表", command=self.select_all).pack(side="right")
        tree_area = ttk.Frame(left, style="Card.TFrame")
        tree_area.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tree_area, columns=("name", "job", "team"), show="headings", selectmode="extended")
        for key, text, width in (("name", "姓名", 155), ("job", "职位", 110), ("team", "球队", 155)):
            self.tree.heading(key, text=text, anchor="w")
            self.tree.column(key, width=width, minwidth=80)
        bar = ttk.Scrollbar(tree_area, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._select)
        self.tree.bind("<Control-a>", lambda _e: (self.select_all(), "break")[-1])
        ttk.Label(left, text="Ctrl / Shift 多选员工；右侧显示当前员工的数据。", style="CardMuted.TLabel",
                  wraplength=370).pack(anchor="w", pady=(8, 0))
        ttk.Label(right, textvariable=self.title, style="Title.TLabel").pack(anchor="w", padx=10)
        ttk.Label(right, textvariable=self.meta, style="Muted.TLabel").pack(anchor="w", padx=10, pady=(2, 8))
        section_bar = ttk.Frame(right)
        section_bar.pack(fill="x", padx=8, pady=(6, 8))
        ttk.Label(section_bar, text="编辑类别").pack(side="left")
        section_choice = tk.StringVar(value="姓名 / 职位")
        section_box = ttk.Combobox(section_bar, textvariable=section_choice, state="readonly", width=24,
                                   values=("姓名 / 职位", *SECTION_ORDER))
        section_box.pack(side="left", padx=10)
        self.app.style.layout("Staff.TNotebook.Tab", [])
        self.tabs = ttk.Notebook(right, style="Staff.TNotebook")
        self.tabs.pack(fill="both", expand=True, padx=8)
        pages = {}
        basic = ScrollFrame(self.tabs)
        self.tabs.add(basic, text="姓名 / 职位")
        pages["姓名 / 职位"] = basic
        for row, (label, variable) in enumerate((("名", self.first_name), ("姓", self.last_name))):
            ttk.Label(basic.inner, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=8)
            ttk.Entry(basic.inner, textvariable=variable, width=28).grid(row=row, column=1, sticky="w", pady=8)
        for row, key in enumerate(("POSITION", "TARGETJOB"), start=2):
            self._make_field(basic.inner, self.field_map[key], row)
        ttk.Label(basic.inner, text="修改姓名或职位后，请重新打开游戏员工页面查看。",
                  style="Muted.TLabel", wraplength=440).grid(row=4, column=0, columnspan=3, sticky="w", pady=12)
        basic.bind_wheel()
        for section in SECTION_ORDER:
            fields = [field for field in self.fields if field["section"] == section
                      and field["id"] not in ("POSITION", "TARGETJOB")]
            area = ScrollFrame(self.tabs)
            self.tabs.add(area, text=section)
            pages[section] = area
            if section == "员工徽章":
                ttk.Label(area.inner, text="等级徽章可选无、铜、银、金、名人堂、传奇；特质徽章使用开关。",
                          style="Muted.TLabel", wraplength=460).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
            elif section in ("能力属性", "阵容熟练度"):
                ttk.Label(area.inner, text="常用评分为 0–100；输入范围按游戏字段存储空间显示。",
                          style="Muted.TLabel", wraplength=460).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
            for row, field in enumerate(fields, start=1):
                self._make_field(area.inner, field, row)
            area.inner.columnconfigure(1, weight=1)
            area.bind_wheel()
        section_box.bind("<<ComboboxSelected>>", lambda _e: self.tabs.select(pages[section_choice.get()]))
        footer = ttk.Frame(right)
        footer.pack(fill="x", padx=8, pady=(8, 0))
        ttk.Button(footer, text="保存当前员工", style="Accent.TButton", command=self.save).pack(side="right")
        ttk.Button(footer, text="撤销上次员工修改", command=self.undo).pack(side="right", padx=8)
        ttk.Label(self, textvariable=self.status, style="Muted.TLabel", wraplength=1100).pack(anchor="w", pady=(9, 0))

    def _make_field(self, parent, field: dict, row: int):
        ttk.Label(parent, text=field["label"]).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=5)
        variable = tk.StringVar()
        self.inputs[field["id"]] = variable
        if field.get("options"):
            control = ttk.Combobox(parent, textvariable=variable, state="readonly", width=25,
                                   values=tuple(field_display(field, value) for value in range(len(field["options"]))))
        else:
            control = ttk.Entry(parent, textvariable=variable, width=17)
        if field.get("readonly"):
            control.configure(state="disabled")
        control.grid(row=row, column=1, sticky="ew", pady=5)
        ttk.Label(parent, text="只读" if field.get("readonly") else f"{field['min']}–{field['max']}",
                  style="Muted.TLabel").grid(row=row, column=2, sticky="w", padx=10, pady=5)

    def reset(self):
        self.people, self.items = [], {}
        self.current, self.baseline, self.last_backup = None, None, None
        self.tree.delete(*self.tree.get_children())
        self.team_box.configure(values=("全部球队",))
        self.team.set("全部球队")
        self.title.set("请选择员工")
        self.meta.set("")
        self.first_name.set("")
        self.last_name.set("")
        for variable in self.inputs.values():
            variable.set("")
        self.count.set("未读取员工")
        self.status.set("等待连接游戏")

    def reload(self, *, confirm=True, reset_backup=False):
        if confirm and not self.confirm_pending("重新读取"):
            return False
        if not self.memory:
            self.reset()
            return False
        selected = self.tree.selection()
        try:
            self.people = self.memory.refresh_staff()
            self.items = {str(person["index"]): person for person in self.people}
            values = ("全部球队", *sorted({person["team_label"] for person in self.people}))
            self.team_box.configure(values=values)
            if self.team.get() not in values:
                self.team.set("全部球队")
            if reset_backup:
                self.last_backup = None
            self.current, self.baseline = None, None
            self._filter(preserve=selected, confirm=False)
            self.status.set(f"已读取 {len(self.people)} 名员工；修改后请在游戏内保存名单或存档。")
            return True
        except Exception as exc:
            self.status.set(str(exc))
            messagebox.showerror("读取员工失败", str(exc), parent=self)
            return False

    def _filter(self, *, preserve=None, confirm=True):
        if self.loading:
            return
        if confirm and not self.confirm_pending("切换员工列表"):
            return
        selected = self.tree.selection() if preserve is None else preserve
        self.loading = True
        try:
            self.tree.delete(*self.tree.get_children())
            term = self.search.get().strip().casefold()
            for person in self.people:
                if self.team.get() != "全部球队" and person["team_label"] != self.team.get():
                    continue
                job = JOBS[person["job"]]
                if term and term not in f"{person['name']} {job} {person['team_label']}".casefold():
                    continue
                self.tree.insert("", "end", iid=str(person["index"]), values=(person["name"], job, person["team_label"]))
            remaining = self.tree.get_children()
            wanted = [iid for iid in selected if iid in remaining]
            if wanted:
                self.tree.selection_set(wanted)
                self.tree.focus(wanted[0])
                self.tree.see(wanted[0])
            elif remaining:
                self.tree.selection_set(remaining[0])
                self.tree.focus(remaining[0])
            self.count.set(f"列表 {len(remaining)} 名员工")
        finally:
            self.loading = False
        self._select()

    def select_all(self):
        self.tree.selection_set(self.tree.get_children())

    def _select(self, _event=None):
        if self.loading:
            return
        selected = self.tree.selection()
        iid = self.tree.focus()
        if iid not in selected:
            iid = selected[0] if selected else ""
        person = self.items.get(iid)
        if self.current and person and self.current["index"] == person["index"]:
            self.meta.set(f"{person['team_label']} · {JOBS[person['job']]} · 已选 {len(selected)} 名员工")
            return
        if self.current and not self.confirm_pending("切换员工"):
            self.loading = True
            self.tree.selection_set(str(self.current["index"]))
            self.tree.focus(str(self.current["index"]))
            self.loading = False
            return
        self.current = person
        if not person or not self.memory:
            self.baseline = None
            self.title.set("请选择员工")
            self.meta.set("")
            self.first_name.set("")
            self.last_name.set("")
            for variable in self.inputs.values():
                variable.set("")
            return
        try:
            row = self.memory.staff_snapshot(person)
            self.baseline = row
            self.first_name.set(person["first_name"])
            self.last_name.set(person["last_name"])
            for field in self.fields:
                self.inputs[field["id"]].set(field_display(field, field_raw(row, field)))
            self.title.set(person["name"])
            self.meta.set(f"{person['team_label']} · {JOBS[person['job']]} · 已选 {len(selected)} 名员工")
        except Exception as exc:
            self.baseline = None
            self.status.set(str(exc))

    def pending(self) -> bool:
        if self.current is None or self.baseline is None:
            return False
        if self.first_name.get() != self.current["first_name"] or self.last_name.get() != self.current["last_name"]:
            return True
        return any(not field.get("readonly") and self.inputs[field["id"]].get() !=
                   field_display(field, field_raw(self.baseline, field)) for field in self.fields)

    def confirm_pending(self, action: str) -> bool:
        if not self.pending():
            return True
        answer = messagebox.askyesnocancel("员工有未保存的修改", f"{self.current['name']} 的修改还未保存。\n\n"
                                          f"是：保存后{action}\n否：放弃修改\n取消：返回", parent=self)
        if answer is None:
            return False
        if answer:
            return self.save()
        self.first_name.set(self.current["first_name"])
        self.last_name.set(self.current["last_name"])
        for field in self.fields:
            self.inputs[field["id"]].set(field_display(field, field_raw(self.baseline, field)))
        return True

    def _pending_values(self) -> tuple[dict[str, int], dict[str, str]]:
        values = {}
        for field in self.fields:
            key = field["id"]
            if not field.get("readonly") and self.inputs[key].get() != field_display(field, field_raw(self.baseline, field)):
                values[key] = parse_field(field, self.inputs[key].get())
        # A job change also updates the requested role unless explicitly edited.
        if "POSITION" in values and "TARGETJOB" not in values:
            values["TARGETJOB"] = values["POSITION"]
        names = {key: variable.get().strip() for key, variable in (("first_name", self.first_name), ("last_name", self.last_name))
                 if variable.get().strip() != self.current[key]}
        return values, names

    def save(self) -> bool:
        if not self.memory or not self.current or self.baseline is None:
            self.status.set("请先选择员工")
            return False
        try:
            values, names = self._pending_values()
            backup = self.memory.apply_staff_many([(self.current, values, names)], self.fields, label=self.current["name"])
            if backup is None:
                self.current, self.baseline = None, None
                self.reload(confirm=False)
                self.status.set("员工数据已是这些值，无需重复保存")
                return True
            self.last_backup = backup
            # Drop the old baseline before reload to avoid prompting for saved edits.
            self.baseline = None
            self.current = None
            self.reload(confirm=False)
            self.status.set("已保存员工修改；可撤销上次员工修改。请在游戏中保存存档。")
            return True
        except Exception as exc:
            messagebox.showerror("保存员工失败", str(exc), parent=self)
            return False

    def undo(self):
        if not self.memory or not self.last_backup:
            self.status.set("没有可撤销的员工修改")
            return
        if not self.confirm_pending("撤销"):
            return
        try:
            self.memory.undo_staff(self.last_backup)
            self.last_backup = None
            self.current, self.baseline = None, None
            self.reload(confirm=False)
            self.status.set("已恢复上次员工修改前的数据")
        except Exception as exc:
            messagebox.showerror("撤销员工失败", str(exc), parent=self)

    def batch_dialog(self):
        if not self.memory or not self.people:
            self.status.set("请先读取员工")
            return
        if not self.confirm_pending("批量修改"):
            return
        dialog = tk.Toplevel(self)
        dialog.title("批量修改员工")
        dialog.geometry("840x680")
        dialog.minsize(680, 560)
        dialog.transient(self.app)
        dialog.grab_set()
        theme.dark_title_bar(dialog)
        body = ttk.Frame(dialog, padding=16)
        body.pack(fill="both", expand=True)
        target = tk.StringVar(value="所选员工")
        category = tk.StringVar(value="能力属性")
        mode = tk.StringVar(value="按各项满值")
        number = tk.StringVar(value="100")
        search = tk.StringVar()
        summary = tk.StringVar()
        top = ttk.Frame(body)
        top.pack(fill="x")
        for label, variable, choices in (("修改对象", target, ("所选员工", "当前球队", "当前联赛", "全部员工")),
                                         ("字段类别", category, ("主要能力和熟练度", "全部可编辑字段", *SECTION_ORDER[:-1]))):
            ttk.Label(top, text=label).pack(side="left")
            ttk.Combobox(top, textvariable=variable, state="readonly", values=choices, width=20).pack(side="left", padx=(8, 16))
        actions = ttk.Frame(body)
        actions.pack(fill="x", pady=(12, 8))
        ttk.Label(actions, text="修改方式").pack(side="left")
        ttk.Combobox(actions, textvariable=mode, state="readonly", width=19,
                     values=("统一数值", "按各项满值", "全部归零", "复制当前员工")).pack(side="left", padx=8)
        number_entry = ttk.Entry(actions, textvariable=number, width=13)
        number_entry.pack(side="left")
        ttk.Label(actions, text="满值：评分 100、等级徽章传奇、开关徽章开启。", style="Muted.TLabel").pack(side="left", padx=10)
        SearchBox(body, search, "搜索要修改的字段…", icons=self.app._icon).pack(fill="x", pady=(0, 8))
        table_area = ttk.Frame(body)
        table_area.pack(fill="both", expand=True)
        table = ttk.Treeview(table_area, columns=("section", "name", "range", "full"), show="headings", selectmode="extended")
        for key, label, width in (("section", "类别", 125), ("name", "字段", 265), ("range", "允许范围", 120), ("full", "满值", 100)):
            table.heading(key, text=label, anchor="w")
            table.column(key, width=width, minwidth=75)
        bar = ttk.Scrollbar(table_area, orient="vertical", command=table.yview)
        table.configure(yscrollcommand=bar.set)
        table.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        toolbar = ttk.Frame(body)
        toolbar.pack(fill="x", pady=(8, 0))
        ttk.Button(toolbar, text="全选当前字段", command=lambda: table.selection_set(table.get_children())).pack(side="left")
        ttk.Button(toolbar, text="清空字段选择", command=lambda: table.selection_remove(table.selection())).pack(side="left", padx=8)
        ttk.Label(toolbar, text="Ctrl / Shift 多选；只写入选中的字段。", style="Muted.TLabel").pack(side="left")
        ttk.Label(body, textvariable=summary, style="Muted.TLabel", wraplength=780).pack(anchor="w", pady=(10, 8))
        buttons = ttk.Frame(body)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side="right")

        def targets() -> list[dict]:
            if target.get() == "所选员工":
                return [self.items[iid] for iid in self.tree.selection() if iid in self.items]
            if target.get() == "当前球队":
                if self.team.get() != "全部球队":
                    return [p for p in self.people if p["team_label"] == self.team.get()]
                return [p for p in self.people if self.current and p["team_ptr"] == self.current["team_ptr"]]
            if target.get() == "当前联赛":
                return [p for p in self.people if self.current and p["league"] == self.current["league"]]
            return self.people

        def update_summary(*_):
            people = targets()
            chosen = table.selection()
            summary.set(f"将对 {len(people)} 名员工的 {len(chosen)} 个字段执行「{mode.get()}」。"
                        "整个批次会先备份，可一次撤销。")
            number_entry.configure(state="normal" if mode.get() == "统一数值" else "disabled")

        def populate(*_):
            table.delete(*table.get_children())
            term = search.get().strip().casefold()
            for field in self.fields:
                if field.get("readonly"):
                    continue
                section = category.get()
                if section == "主要能力和熟练度" and field["section"] not in ("能力属性", "阵容熟练度"):
                    continue
                if section not in ("全部可编辑字段", "主要能力和熟练度") and field["section"] != section:
                    continue
                if term and term not in f"{field['label']} {field['id']}".casefold():
                    continue
                table.insert("", "end", iid=field["id"], values=(field["section"], field["label"],
                               f"{field['min']}–{field['max']}", field_display(field, field["full_value"])))
            table.selection_set(table.get_children())
            update_summary()

        def apply():
            try:
                people = targets()
                keys = table.selection()
                if not people or not keys:
                    raise ValueError("请选择目标员工和要修改的字段")
                if mode.get() == "统一数值":
                    values = {key: parse_field(self.field_map[key], number.get()) for key in keys}
                elif mode.get() == "全部归零":
                    values = {key: 0 for key in keys}
                elif mode.get() == "按各项满值":
                    values = {key: self.field_map[key]["full_value"] for key in keys}
                else:
                    if not self.current:
                        raise ValueError("请先选择作为来源的员工")
                    row = self.memory.staff_snapshot(self.current)
                    values = {key: field_raw(row, self.field_map[key]) for key in keys}
                if "POSITION" in values and "TARGETJOB" not in values:
                    values["TARGETJOB"] = values["POSITION"]
                backup = self.memory.apply_staff_many([(person, values, {}) for person in people], self.fields,
                                                        label=f"{len(people)}名员工 · {mode.get()} · {len(keys)}项")
                if backup is not None:
                    self.last_backup = backup
                self.current, self.baseline = None, None
                self.reload(confirm=False)
                self.status.set(f"已修改 {len(people)} 名员工的 {len(keys)} 个字段；可一次撤销。" if backup else
                                "所有目标员工已是这些值，没有重复写入。")
                dialog.destroy()
            except Exception as exc:
                messagebox.showerror("批量修改员工失败", str(exc), parent=dialog)

        ttk.Button(buttons, text="应用批量修改", style="Accent.TButton", command=apply).pack(side="right", padx=8)
        category.trace_add("write", populate)
        search.trace_add("write", populate)
        target.trace_add("write", update_summary)
        mode.trace_add("write", update_summary)
        table.bind("<<TreeviewSelect>>", update_summary)
        table.bind("<Control-a>", lambda _e: (table.selection_set(table.get_children()), "break")[-1])
        dialog.bind("<Escape>", lambda _e: dialog.destroy())
        populate()
