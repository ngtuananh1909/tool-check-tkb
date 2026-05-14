import re


MANUAL_ALIASES: dict[str, str] = {
}


def shorten_course_name(full_name: str, aliases: dict[str, str] | None = None) -> str:
    name = re.sub(r"\s+", " ", str(full_name or "").strip())
    if not name:
        return "Môn học"

    merged_aliases = dict(MANUAL_ALIASES)
    if aliases:
        merged_aliases.update(aliases)
    if name in merged_aliases:
        return merged_aliases[name]

    text = re.sub(r"^HK\d+_\d{4}_\d{5,}_", "", name, flags=re.IGNORECASE)
    text = re.sub(r"^[A-Z0-9_.-]{3,}\s*[-–:]\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*[-–:]\s*(nhóm|nhom|group|lớp|lop)\s*\w+\s*$", "", text, flags=re.IGNORECASE)
    text = text.strip(" -–:")
    if len(text) <= 64:
        return text
    return text[:61].rstrip() + "..."
