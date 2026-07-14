"""Lossless helpers for UE4SS Mods/mods.txt entries."""

from __future__ import annotations

import re
from collections.abc import Iterable

_ENTRY = re.compile(
    r"^(?P<prefix>\s*)(?P<name>[^:#;]+?)(?P<separator>\s*:\s*)"
    r"(?P<value>[01])(?P<tail>\s*(?:[#;].*)?)$"
)


def _validated_name(name: str) -> str:
    if not isinstance(name, str) or not name.strip() or any(char in name for char in "\r\n:#;"):
        raise ValueError("UE4SS mod name is invalid")
    return name.strip()


def parse_entry(line: str) -> tuple[str, str] | None:
    """Parse one mods.txt line without accepting comments or loose syntax."""
    candidate = line[1:] if line.startswith("\ufeff") else line
    match = _ENTRY.match(candidate)
    if not match:
        return None
    return match.group("name").strip(), match.group("value")


def _split_line(line: str) -> tuple[str, str]:
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith(("\n", "\r")):
        return line[:-1], line[-1]
    return line, ""


def _newline(lines: list[str]) -> str:
    for line in lines:
        _content, ending = _split_line(line)
        if ending:
            return ending
    return "\n"


def enabled_state(data: bytes, name: str) -> bool | None:
    wanted = _validated_name(name).casefold()
    for line in data.decode("utf-8").splitlines():
        entry = parse_entry(line)
        if entry is not None and entry[0].casefold() == wanted:
            return entry[1] == "1"
    return None


def update_entry(data: bytes, name: str, enabled: bool) -> bytes:
    if type(enabled) is not bool:
        raise TypeError("enabled must be a bool")
    validated = _validated_name(name)
    wanted = validated.casefold()
    lines = data.decode("utf-8").splitlines(keepends=True)
    newline = _newline(lines)
    found = False
    output: list[str] = []
    for line in lines:
        content, ending = _split_line(line)
        entry = parse_entry(content)
        if entry is None or entry[0].casefold() != wanted:
            output.append(line)
            continue
        if found:
            continue
        output.append(
            re.sub(r"(\s*:\s*)[01]", rf"\g<1>{int(enabled)}", content, count=1)
            + ending
        )
        found = True
    if not found:
        if output and not output[-1].endswith(("\r\n", "\n", "\r")):
            output[-1] += newline
        output.append(f"{validated} : {int(enabled)}{newline}")
    return "".join(output).encode("utf-8")


def remove_entry(data: bytes, name: str) -> bytes:
    return remove_entries(data, {_validated_name(name)})


def remove_entries(data: bytes, names: Iterable[str]) -> bytes:
    wanted = {_validated_name(name).casefold() for name in names}
    output: list[str] = []
    for line in data.decode("utf-8").splitlines(keepends=True):
        content, _ending = _split_line(line)
        entry = parse_entry(content)
        if entry is not None and entry[0].casefold() in wanted:
            continue
        output.append(line)
    return "".join(output).encode("utf-8")


def merge_missing_entries(data: bytes, bundled: bytes, names: Iterable[str]) -> bytes:
    wanted = {_validated_name(name).casefold() for name in names}
    merged = data
    seen: set[str] = set()
    for line in bundled.decode("utf-8").splitlines():
        entry = parse_entry(line)
        if entry is None:
            continue
        name, value = entry
        key = name.casefold()
        if key not in wanted or key in seen:
            continue
        seen.add(key)
        if enabled_state(merged, name) is None:
            merged = update_entry(merged, name, value == "1")
    return merged
