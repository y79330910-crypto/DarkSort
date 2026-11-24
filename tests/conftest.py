import pytest

from darksort.models import Mod, Source


@pytest.fixture
def mods(tmp_path):
    return [
        Mod(
            str(tmp_path / "workshop" / "1001"),
            Source.WORKSHOP,
            "甲 Mod",
            workshop_id="1001",
            tags=("英雄",),
        ),
        Mod(str(tmp_path / "workshop" / "1002"), Source.WORKSHOP, "乙 Mod", workshop_id="1002"),
        Mod(str(tmp_path / "local" / "old"), Source.LOCAL, "旧版 Mod"),
    ]
