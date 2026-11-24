"""Read DSON structure and replace the manifested Mod list while retaining opaque data.

Layout reference: https://github.com/robojumper/DarkestDungeonSaveEditor/blob/master/docs/dson.md
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, replace

MAGIC = b"\x01\xb1\x00\x00"
HEADER = struct.Struct("<16I")
OBJECT = struct.Struct("<iIII")
FIELD = struct.Struct("<III")
NAME_MASK = 0x7FC
OBJECT_INDEX_MASK = 0xFFFFF
MAX_FIELDS = 100000
MAX_NESTING = 96
VALIDATED_REVISIONS = frozenset({28385})
UGCS = "applied_ugcs_1_0"


@dataclass(frozen=True)
class SavedMod:
    name: str
    source: str

    @property
    def key(self) -> tuple[str, str]:
        return self.source.casefold(), self.name.casefold()


@dataclass(frozen=True)
class DsonField:
    name: str
    info: int = 0
    body_offset: int = -1
    raw: bytes = b""
    children: tuple[DsonField, ...] | None = None

    def one(self, name: str) -> DsonField:
        matches = [field for field in self.children or () if field.name == name]
        if len(matches) != 1:
            raise ValueError(f"DSON 字段 {self.name}/{name} 缺失或重复。")
        return matches[0]

    def string(self) -> str:
        payload = self.raw[(-self.body_offset) % 4 :]
        if (
            self.children is not None or len(payload) < 5
            or struct.unpack_from("<I", payload)[0] != len(payload) - 4
            or payload[-1] != 0 or b"\0" in payload[4:-1]
        ):
            raise ValueError(f"DSON 字段 {self.name} 不是受支持的字符串。")
        return payload[4:-1].decode("utf-8")


def name_hash(name: bytes) -> int:
    value = 0
    for byte in name:
        value = (value * 53 + byte) & 0xFFFFFFFF
    return value


@dataclass(frozen=True)
class DsonDocument:
    header: tuple[int, ...]
    roots: tuple[DsonField, ...]
    canonical_offsets: bool = True

    @property
    def revision(self) -> int:
        return self.header[1] >> 16

    @property
    def base(self) -> DsonField:
        return DsonField("", children=self.roots).one("base_root")

    @classmethod
    def read(cls, data: bytes) -> DsonDocument:
        if len(data) < HEADER.size or not data.startswith(MAGIC):
            raise ValueError("DSON 文件头无效。")
        header = HEADER.unpack_from(data)
        _, _, header_length, _, object_size, object_count, object_start = header[:7]
        field_count, field_start, _, data_length, data_start = header[11:]
        if (
            header_length != HEADER.size or object_start != header_length
            or not 0 < object_count <= field_count <= MAX_FIELDS
            or object_size != object_count * OBJECT.size
            or field_start != object_start + object_size
            or data_start != field_start + field_count * FIELD.size
            or data_start + data_length != len(data)
        ):
            raise ValueError("DSON 表长度或偏移不一致。")
        fields = list(FIELD.iter_unpack(memoryview(data)[field_start:data_start]))
        objects = list(OBJECT.iter_unpack(memoryview(data)[object_start:field_start]))
        offsets = [field[1] for field in fields]
        ordered = sorted(offsets)
        if ordered[0] != 0 or ordered[-1] >= data_length or len(set(ordered)) != field_count:
            raise ValueError("DSON 数据块存在重复或越界的字段偏移。")
        ends = dict(zip(ordered, ordered[1:] + [data_length], strict=True))
        seen = set()
        cursor = 0

        def read_field(parent: int, depth: int) -> DsonField:
            nonlocal cursor
            if cursor >= field_count or depth >= MAX_NESTING:
                raise ValueError("DSON 字段数量或嵌套深度异常。")
            index = cursor
            hashed, offset, info = fields[cursor]
            cursor += 1
            end = ends[offset]
            body = offset + ((info & NAME_MASK) >> 2)
            if body <= offset or body > end or data[data_start + body - 1] != 0:
                raise ValueError(f"DSON 字段 {index} 的名称长度无效。")
            name_bytes = data[data_start + offset : data_start + body - 1]
            if b"\0" in name_bytes or name_hash(name_bytes) != hashed:
                raise ValueError(f"DSON 字段 {index} 的名称校验失败。")
            name = name_bytes.decode("utf-8")
            raw = data[data_start + body : data_start + end]
            children = None
            if info & 1:
                object_index = (info >> 11) & OBJECT_INDEX_MASK
                if object_index >= object_count or object_index in seen:
                    raise ValueError(f"DSON 对象 {name} 的索引无效。")
                seen.add(object_index)
                expected_parent, expected_field, direct, descendants = objects[object_index]
                if (
                    expected_parent != parent or expected_field != index
                    or direct > descendants or descendants > field_count - cursor
                ):
                    raise ValueError(f"DSON 对象 {name} 的层级无效。")
                children = tuple(read_field(object_index, depth + 1) for _ in range(direct))
                if cursor - index - 1 != descendants:
                    raise ValueError(f"DSON 对象 {name} 的后代数不一致。")
            elif parent < 0:
                raise ValueError("DSON 根字段必须是对象。")
            return DsonField(name, info, body, raw, children)

        roots = []
        while cursor < field_count:
            roots.append(read_field(-1, 0))
        if len(seen) != object_count:
            raise ValueError("DSON 存在未引用的对象。")
        return cls(header, tuple(roots), offsets == ordered)

    def mods(self) -> tuple[SavedMod, ...]:
        ugcs = self.base.one(UGCS)
        if ugcs.children is None:
            raise ValueError("存档的启用列表不是数字键对象。")
        entries = {}
        for field in ugcs.children:
            if not field.name.isascii() or not field.name.isdecimal() or len(field.name) > 6:
                raise ValueError("存档的 Mod 序号无效。")
            order = int(field.name)
            if order in entries:
                raise ValueError("存档的 Mod 序号重复。")
            entries[order] = SavedMod(field.one("name").string(), field.one("source").string())
        return tuple(entries[order] for order in sorted(entries))

    def write_reason(self) -> str:
        if self.revision not in VALIDATED_REVISIONS:
            return f"DSON build {self.revision} 可读取。写入验证范围为 build 28385。"
        if not self.canonical_offsets:
            return "该 DSON 的数据偏移顺序需要额外验证。"
        in_raid = self.base.one("inraid")
        if in_raid.children is not None or in_raid.raw != b"\0":
            return "请在城镇退出游戏并保存后再应用。"
        current = self.mods()
        if any(not supported_mod(mod) for mod in current) or len({m.key for m in current}) != len(current):
            return "存档包含重复或尚未支持的 Mod 来源。"
        if any(
            set(child.name for child in row.children or ()) != {"name", "source"}
            for row in self.base.one(UGCS).children or ()
        ):
            return "存档 Mod 条目含有额外字段，需要额外验证。"
        return ""

    def with_mods(self, mods: tuple[SavedMod, ...]) -> DsonDocument:
        base = self.base
        ugcs = base.one(UGCS)
        children = []
        for index, mod in enumerate(mods):
            values = []
            for name, text in (("name", mod.name), ("source", mod.source)):
                data = text.encode("utf-8") + b"\0"
                values.append(DsonField(name, raw=struct.pack("<I", len(data)) + data))
            children.append(DsonField(str(index), children=tuple(values)))
        updated = replace(ugcs, children=tuple(children))
        new_base = replace(base, children=tuple(updated if field is ugcs else field for field in base.children))
        return replace(self, roots=tuple(new_base if field is base else field for field in self.roots))

    def encode(self) -> bytes:
        data = bytearray()
        fields = []
        objects = []

        def emit(field: DsonField, parent: int) -> None:
            name = field.name.encode("utf-8")
            offset = len(data)
            index = len(fields)
            info = (field.info & ~NAME_MASK) | ((len(name) + 1) << 2)
            if field.children is not None:
                object_index = len(objects)
                info = (info & 0x800007FE) | 1 | (object_index << 11)
                objects.append(None)
            fields.append(FIELD.pack(name_hash(name), offset, info))
            data.extend(name + b"\0")
            data.extend(relocate(field, len(data)))
            if field.children is not None:
                for child in field.children:
                    emit(child, object_index)
                objects[object_index] = OBJECT.pack(
                    parent, index, len(field.children), len(fields) - 1 - index
                )

        for field in self.roots:
            emit(field, -1)
        header = list(self.header)
        header[4] = len(objects) * OBJECT.size
        header[5] = len(objects)
        header[11] = len(fields)
        header[12] = HEADER.size + header[4]
        header[14] = len(data)
        header[15] = header[12] + len(fields) * FIELD.size
        return b"".join((HEADER.pack(*header), b"".join(objects), b"".join(fields), data))

    def outside_mods(self) -> tuple:
        """Compare structure and bytes of all unrelated fields, allowing only alignment changes."""
        base, ugcs = self.base, self.base.one(UGCS)

        def contents(field: DsonField) -> tuple:
            if field is ugcs:
                return (field.name, field.info & 0x80000003, field.raw)
            if field.children is not None:
                return (
                    field.name, field.info & 0x80000003, field.raw,
                    tuple(contents(child) for child in field.children),
                )
            raw = field.raw
            if len(raw) > 1:
                pad = (-field.body_offset) % 4
                if len(raw) >= pad + 4 and not any(raw[:pad]):
                    raw = raw[pad:]
            return (field.name, field.info, raw)

        stable_header = tuple(self.header[i] for i in (0, 1, 2, 3, 6, 7, 8, 9, 10, 13))
        return stable_header, base.name, tuple(contents(field) for field in self.roots)


def relocate(field: DsonField, offset: int) -> bytes:
    if field.children is not None:
        if field.raw and offset % 4 != field.body_offset % 4:
            raise ValueError(f"对象 {field.name} 含有未知对齐数据，无法安全移动。")
        return field.raw
    if field.body_offset < 0:
        return b"\0" * ((-offset) % 4) + field.raw
    if offset % 4 == field.body_offset % 4 or len(field.raw) == 1:
        return field.raw
    padding = (-field.body_offset) % 4
    if len(field.raw) < padding + 4 or any(field.raw[:padding]):
        raise ValueError(f"字段 {field.name} 含有未知对齐数据，无法安全移动。")
    return b"\0" * ((-offset) % 4) + field.raw[padding:]


def supported_mod(mod: SavedMod) -> bool:
    if not mod.name.strip() or "\0" in mod.name or len(mod.name.encode("utf-8")) > 512:
        return False
    if mod.source == "Steam":
        return (
            mod.name.isascii() and mod.name.isdecimal() and len(mod.name) <= 20
            and 0 < int(mod.name) < 2**64 and str(int(mod.name)) == mod.name
        )
    return mod.source == "mod_local_source"
