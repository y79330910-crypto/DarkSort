from urllib.parse import parse_qs, urlsplit


def workshop_id(value: str | None) -> str | None:
    if isinstance(value, str) and len(value) <= 20 and value.isascii() and value.isdecimal():
        if 0 < (number := int(value)) < 2**64:
            return str(number)
    return None


def workshop_page(identifier: str | None) -> str:
    if (number := workshop_id(identifier)) is None:
        return ""
    return f"https://steamcommunity.com/sharedfiles/filedetails/?id={number}"


def workshop_link_id(value: str) -> str | None:
    try:
        url = urlsplit(value.strip())
    except ValueError:
        return None
    host = url.netloc.casefold()
    if url.scheme == "steam" and host == "url":
        page, _, identifier = url.path.rstrip("/").rpartition("/")
        return workshop_id(identifier) if page.casefold() == "/communityfilepage" else None
    if url.scheme not in ("http", "https") or host not in ("steamcommunity.com", "www.steamcommunity.com"):
        return None
    if url.path.rstrip("/").casefold() not in ("/sharedfiles/filedetails", "/workshop/filedetails"):
        return None
    ids = parse_qs(url.query, keep_blank_values=True).get("id", [])
    return workshop_id(ids[0]) if len(ids) == 1 else None
