"""Read-only verification of one explicitly selected decision export."""
from __future__ import annotations

import json
import os
import stat
from typing import BinaryIO


def _unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError
        obj[key] = value
    return obj


def _invalid_number(_text):
    raise ValueError


def _snapshot(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _within_depth(raw: bytes, limit: int) -> bool:
    depth = 0
    quoted = escaped = False
    for char in raw:
        if quoted:
            if escaped:
                escaped = False
            elif char == 92:  # backslash
                escaped = True
            elif char == 34:  # double quote
                quoted = False
        elif char == 34:
            quoted = True
        elif char in (123, 91):  # object or array opening
            depth += 1
            if depth > limit:
                return False
        elif char in (125, 93):
            depth -= 1
    return True


def _scan(file: BinaryIO, size: int, original_id: str, limits) -> str:
    consumed = matches = rows = 0
    # Only string IDs are eligible. All other JSON numbers can remain opaque.
    number = object()
    while True:
        raw = file.readline(limits["lineBytes"] + 3)
        if not raw:
            break
        consumed += len(raw)
        rows += 1
        if consumed > size or rows > limits["rows"]:
            return "file_check_incomplete"
        if raw.endswith(b"\n"):
            raw = raw[:-1].removesuffix(b"\r")
        if len(raw) > limits["lineBytes"] or not _within_depth(raw, limits["depth"]):
            return "file_check_incomplete"
        obj = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_int=lambda _: number,
                         parse_float=lambda _: number, parse_constant=_invalid_number)
        if not isinstance(obj, dict) or not isinstance(obj.get("decision_id"), str) or not obj["decision_id"]:
            return "file_check_incomplete"
        matches += obj["decision_id"] == original_id
    if consumed != size:
        return "file_check_incomplete"
    return ("file_check_none", "file_check_one", "file_check_multiple")[min(matches, 2)]


def verify(path, original_id: str, limits) -> str:
    """A definitive answer requires a complete read of a single, unchanged regular file."""
    if not original_id:
        return "file_check_incomplete"
    try:
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limits["fileBytes"]:
            return "file_check_incomplete"
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        with os.fdopen(os.open(path, flags), "rb") as file:
            first = os.fstat(file.fileno())
            if not stat.S_ISREG(first.st_mode) or _snapshot(first) != _snapshot(before):
                return "file_check_incomplete"
            result = _scan(file, first.st_size, original_id, limits)
            if _snapshot(os.fstat(file.fileno())) == _snapshot(first) == _snapshot(os.lstat(path)):
                return result
    except (OSError, ValueError, RecursionError, OverflowError):
        pass
    return "file_check_incomplete"
