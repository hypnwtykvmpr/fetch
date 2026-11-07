"""The generated emoji table. Tests never read scripts/."""

import hashlib
import re

from mcp_server_fetch import _emoji_ranges as t


def _in(ranges, cp: int) -> bool:
    return any(lo <= cp <= hi for lo, hi in ranges)


def test_table_matches_its_recorded_hash() -> None:
    body = repr((t.EXTENDED_PICTOGRAPHIC, t.EMOJI_MODIFIER)).encode()
    assert hashlib.sha256(body).hexdigest() == t.TABLE_SHA256


def test_ranges_are_sorted_merged_and_disjoint() -> None:
    for ranges in (t.EXTENDED_PICTOGRAPHIC, t.EMOJI_MODIFIER):
        prev = -2
        for lo, hi in ranges:
            assert lo <= hi and lo > prev + 1
            prev = hi


def test_known_members() -> None:
    for cp in (0x1F600, 0x00A9, 0x2764):
        assert _in(t.EXTENDED_PICTOGRAPHIC, cp)
    for cp in range(0x1F3FB, 0x1F400):
        assert _in(t.EMOJI_MODIFIER, cp)
    for cp in (0x41, 0x1F1E6, 0x30):
        assert not _in(t.EXTENDED_PICTOGRAPHIC, cp) and not _in(t.EMOJI_MODIFIER, cp)


def test_version_and_source_are_recorded() -> None:
    assert re.fullmatch(r"\d+\.\d+(\.\d+)?", t.UNICODE_EMOJI_VERSION)
    assert re.fullmatch(r"[0-9a-f]{64}", t.SOURCE_SHA256)
