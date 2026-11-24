from pathlib import Path

from darksort.storage import atomic_json, read_json


class TagStore:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "tags.json"
        self.tags: dict[str, tuple[str, ...]] = {}
        if self.path.is_file():
            data = read_json(self.path)
            for key, tags in data.items():
                if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
                    raise ValueError("自定义标签文件包含无效条目。")
                self.tags[key] = tuple(tags)

    def set(self, reference: str, text: str) -> None:
        self.set_many((reference,), text)

    def set_many(self, references, text: str, *, append=False) -> None:
        tags = {}
        for name in text.replace("，", ",").split(","):
            name = name.strip()
            if name:
                if len(name) > 32:
                    raise ValueError("每个标签最多 32 个字符。")
                tags.setdefault(name.casefold(), name)
        updated = self.tags.copy()
        for reference in references:
            previous = {tag.casefold(): tag for tag in updated.get(reference, ())} if append else {}
            ordered = previous | {key: name for key, name in tags.items() if key not in previous}
            if len(ordered) > 24:
                raise ValueError("一个 Mod 最多可添加 24 个自定义标签。")
            if ordered:
                updated[reference] = tuple(ordered.values())
            else:
                updated.pop(reference, None)
        atomic_json(self.path, updated)
        self.tags = updated
