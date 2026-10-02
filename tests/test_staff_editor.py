"""Verify packed staff fields, batch validation, rollback and roster identity."""
from pathlib import Path
import random
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nba2k27_player_editor as editor
import staff_editor as staff


FIELDS = staff.load_fields()
BY_ID = {field["id"]: field for field in FIELDS}


class FakeMemory(editor.GameMemory):
    def __init__(self):
        self.pid = 42
        self.staff_roster, self.staff_count, self.staff_table = 2000, 2, 8000
        self.layout = (self.staff_roster, self.staff_count, self.staff_table)
        self.data = bytearray(staff.STAFF_SIZE * 2)
        self.writes = 0
        self.fail_once = False
        self.people = []
        for index in range(2):
            offset = index * staff.STAFF_SIZE
            struct.pack_into("<H", self.data, offset + staff.STAFF_UID_OFFSET, index + 10)
            self.people.append({"index": index, "uid": index + 10, "team_ptr": 0,
                                "address": self.staff_table + offset})

    def _staff_layout(self):
        return self.layout

    def read(self, address, size):
        offset = address - self.staff_table
        if offset < 0 or offset + size > len(self.data):
            raise RuntimeError("outside fake table")
        return bytes(self.data[offset:offset + size])

    def write_verified(self, address, value):
        offset = address - self.staff_table
        self.data[offset:offset + len(value)] = value
        self.writes += 1
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("simulated partial write")


class PackedFieldsTest(unittest.TestCase):
    def test_all_editable_fields_preserve_unselected_bits(self):
        rng = random.Random(271)
        row = rng.randbytes(staff.STAFF_SIZE)
        for field in FIELDS:
            if field.get("readonly"):
                continue
            with self.subTest(field=field["id"]):
                value = field["full_value"]
                parts = staff.build_changes(row, FIELDS, {field["id"]: value})
                updated = bytearray(row)
                for offset, data in parts.items():
                    updated[offset:offset + len(data)] = data
                self.assertEqual(staff.field_raw(updated, field), value)
                mask = ((1 << field["bits"]) - 1) << (field["offset"] * 8 + field["shift"])
                delta = int.from_bytes(row, "little") ^ int.from_bytes(updated, "little")
                self.assertEqual(delta & ~mask, 0)
                self.assertEqual(staff.build_changes(updated, FIELDS, {field["id"]: value}), {})

    def test_multiple_badges_sharing_bytes(self):
        badges = [field for field in FIELDS if field["section"] == "员工徽章" and not field.get("readonly")]
        row = random.Random(272).randbytes(staff.STAFF_SIZE)
        values = {field["id"]: field["full_value"] for field in badges}
        updated = bytearray(row)
        for offset, data in staff.build_changes(row, FIELDS, values).items():
            updated[offset:offset + len(data)] = data
        for field in badges:
            self.assertEqual(staff.field_raw(updated, field), field["full_value"], field["id"])

    def test_ranges_readonly_and_names(self):
        row = bytes(staff.STAFF_SIZE)
        for values in ({"UNIQUEID": 1}, {"BALANCEDPROFICIENCY": 256}, {"BIGMANCOACHBADGE": 6}, {"unknown": 1}):
            with self.assertRaises(ValueError):
                staff.build_changes(row, FIELDS, values)
        parts = staff.build_changes(row, FIELDS, {}, {"first_name": "教练", "last_name": "测试"})
        updated = bytearray(row)
        for offset, data in parts.items():
            updated[offset:offset + len(data)] = data
        self.assertEqual(editor.decode_name(updated[80:120]), "教练")
        self.assertEqual(editor.decode_name(updated[120:160]), "测试")
        with self.assertRaises(ValueError):
            staff.name_bytes("a" * 20)


class BatchWriteTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.patch = patch.object(editor, "BACKUP_DIR", Path(self.directory.name))
        self.patch.start()
        self.memory = FakeMemory()

    def tearDown(self):
        self.patch.stop()
        self.directory.cleanup()

    def changes(self):
        return [(person, {"BALANCEDPROFICIENCY": 100, "BIGMANCOACHBADGE": 5}, {}) for person in self.memory.people]

    def test_batch_undo_and_idempotence(self):
        before = bytes(self.memory.data)
        backup = self.memory.apply_staff_many(self.changes(), FIELDS, label="test")
        writes = self.memory.writes
        self.assertIsNone(self.memory.apply_staff_many(self.changes(), FIELDS, label="same"))
        self.assertEqual(self.memory.writes, writes)
        self.memory.undo_staff(backup)
        self.assertEqual(bytes(self.memory.data), before)

    def test_invalid_second_target_prevents_all_writes(self):
        self.memory.people[1]["uid"] = 999
        before = bytes(self.memory.data)
        with self.assertRaises(RuntimeError):
            self.memory.apply_staff_many(self.changes(), FIELDS, label="invalid")
        self.assertEqual(self.memory.writes, 0)
        self.assertEqual(bytes(self.memory.data), before)

    def test_switched_roster_and_conflicting_undo(self):
        backup = self.memory.apply_staff_many(self.changes(), FIELDS, label="test")
        self.memory.layout = (2001, 2, 8000)
        with self.assertRaises(RuntimeError):
            self.memory.undo_staff(backup)
        self.memory.layout = (2000, 2, 8000)
        self.memory.data[368] = 99
        with self.assertRaises(RuntimeError):
            self.memory.undo_staff(backup)

    def test_partial_write_is_rolled_back(self):
        before = bytes(self.memory.data)
        self.memory.fail_once = True
        with self.assertRaises(RuntimeError):
            self.memory.apply_staff_many(self.changes(), FIELDS, label="fail")
        self.assertEqual(bytes(self.memory.data), before)


if __name__ == "__main__":
    unittest.main()
