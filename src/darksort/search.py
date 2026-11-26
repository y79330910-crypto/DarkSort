from dataclasses import dataclass
from unicodedata import normalize


def search_text(value: str) -> str:
    return " ".join(normalize("NFKC", value).casefold().split())


def unique_title(original: str, existing, *, limit: int = 120) -> str:
    taken = {search_text(title) for title in existing}
    title = original
    number = 2
    while search_text(title) in taken:
        suffix = f" ({number})"
        title = original[:limit - len(suffix)].rstrip() + suffix
        number += 1
    return title


@dataclass(frozen=True)
class ModLocation:
    side: str
    identity: str


@dataclass(frozen=True)
class SearchRecord:
    target: ModLocation
    title: str
    location: str
    titles: tuple[str, ...]
    workshop_id: str
    metadata: str
    details: str

    @classmethod
    def create(cls, target, title, location, names, workshop_id, metadata, details):
        return cls(
            target, title, location, tuple(search_text(name) for name in names),
            workshop_id or "", search_text(metadata), search_text(details),
        )

    def rank(self, query: str) -> int | None:
        if query == self.workshop_id:
            return 0
        if query in self.titles:
            return 1
        if any(title.startswith(query) for title in self.titles):
            return 2
        tokens = query.split()
        if all(any(token in title for title in self.titles) for token in tokens):
            return 3
        fields = (*self.titles, self.workshop_id, self.metadata)
        if all(any(token in field for field in fields) for token in tokens):
            return 4
        if all(any(token in field for field in (*fields, self.details)) for token in tokens):
            return 5
        return None
