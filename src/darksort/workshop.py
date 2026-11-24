def workshop_id(value: str | None) -> str | None:
    if isinstance(value, str) and len(value) <= 20 and value.isascii() and value.isdecimal():
        if 0 < (number := int(value)) < 2**64:
            return str(number)
    return None


def workshop_page(identifier: str | None) -> str:
    if (number := workshop_id(identifier)) is None:
        return ""
    return f"https://steamcommunity.com/sharedfiles/filedetails/?id={number}"
