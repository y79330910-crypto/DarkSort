from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from darksort.storage import atomic_json, read_json

DEFAULT_THEME = "nightfall"
MAX_SETTINGS_BYTES = 64 * 1024 * 1024
MAX_MOD_NOTE = 500
MARK_COLORS = (
    ("蓝色", "#75b9ea"),
    ("紫色", "#bd99e8"),
    ("绿色", "#74c7aa"),
    ("金色", "#e3b574"),
    ("粉色", "#e49eb4"),
    ("青色", "#61c8cd"),
    ("橙色", "#e69a69"),
    ("红色", "#df818b"),
)


@dataclass
class Settings:
    game_path: str = ""
    workshop_paths: list[str] = field(default_factory=list)
    local_paths: list[str] = field(default_factory=list)
    steam_paths: list[str] = field(default_factory=list)
    last_profile: str = ""
    auto_detect: bool = True
    theme: str = DEFAULT_THEME
    mod_categories: dict[str, str] = field(default_factory=dict)
    mod_colors: dict[str, str] = field(default_factory=dict)
    mod_aliases: dict[str, str] = field(default_factory=dict)
    mod_translations: dict[str, str] = field(default_factory=dict)
    ignored_problems: dict[str, list[str]] = field(default_factory=dict)
    excluded_paths: list[str] = field(default_factory=list)
    mod_workshop_links: dict[str, str] = field(default_factory=dict)
    mod_notes: dict[str, str] = field(default_factory=dict)
    pane_sizes: list[int] = field(default_factory=list)
    mod_menu_mode: str = "full"
    mod_menu_items: dict[str, list[str]] = field(default_factory=dict)
    campaign_launch_options: dict[str, dict[str, str]] = field(default_factory=dict)

    @classmethod
    def load(cls, data_dir: Path) -> Settings:
        file = data_dir / "settings.json"
        if not file.exists():
            return cls()
        data = read_json(file, max_bytes=MAX_SETTINGS_BYTES)
        for key in ("game_path", "last_profile", "theme"):
            if key in data and not isinstance(data[key], str):
                raise ValueError(f"设置字段 {key} 无效。")
        for key in ("steam_paths", "workshop_paths", "local_paths", "excluded_paths"):
            value = data.get(key, [])
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ValueError(f"设置字段 {key} 无效。")
        if "auto_detect" in data and not isinstance(data["auto_detect"], bool):
            raise ValueError("自动检测设置无效。")
        for key in ("mod_categories", "mod_colors", "mod_aliases", "mod_translations",
                    "mod_workshop_links", "mod_notes"):
            values = data.get(key, {})
            if not isinstance(values, dict) or not all(isinstance(value, str) for value in values.values()):
                raise ValueError(f"设置字段 {key} 无效。")
        sizes = data.get("pane_sizes", [])
        if not isinstance(sizes, list) or (sizes and (
            len(sizes) != 4 or not all(type(size) is int and 0 <= size <= 32767 for size in sizes)
            or not any(sizes)
        )):
            raise ValueError("栏宽设置无效。")
        if data.get("mod_menu_mode") not in ("quick", "full"):
            data.pop("mod_menu_mode", None)
        menu_items = data.get("mod_menu_items", {})
        data["mod_menu_items"] = {
            side: list(dict.fromkeys(items))
            for side, items in menu_items.items()
            if side in ("library", "profile", "group") and isinstance(items, list)
            and all(isinstance(item, str) for item in items)
        } if isinstance(menu_items, dict) else {}
        if data.get("mod_menu_mode") == "quick" and not data["mod_menu_items"]:
            # Earlier versions stored the old default even before the menu settings were ever opened.
            data.pop("mod_menu_mode")
        campaign_options = data.get("campaign_launch_options", {})
        data["campaign_launch_options"] = {
            key: value
            for key, value in campaign_options.items()
            if isinstance(value, dict) and value.get("mode") in ("steam", "manual")
            and isinstance(value.get("manual"), str)
        } if isinstance(campaign_options, dict) else {}
        ignored = data.get("ignored_problems", {})
        if not isinstance(ignored, dict) or any(
            not isinstance(keys, list) or any(
                not isinstance(key, str) or len(key) != 64 for key in keys
            )
            for keys in ignored.values()
        ):
            raise ValueError("忽略提醒设置无效。")
        return cls(**{key: data[key] for key in cls.__dataclass_fields__ if key in data})

    def save(self, data_dir: Path) -> None:
        from dataclasses import asdict

        atomic_json(data_dir / "settings.json", asdict(self), max_bytes=MAX_SETTINGS_BYTES)
