"""Date foldering and the collision scheme that keeps resume working."""

from zoneinfo import ZoneInfoNotFoundError

import pytest

from gopro_dl.paths import (
    CaptureDateError,
    date_folder,
    parse_timezone,
    safe_filename,
    suffixed,
    target_path,
)


def test_capture_local_date_wins_over_utc():
    # 22:30 UTC is already the next day in Brussels -- the folder must follow
    # the capture-local date, not UTC.
    assert date_folder("2023-07-14T22:30:00Z", "Europe/Brussels")[0] == "2023-07-15"
    assert date_folder("2023-07-14T22:30:00Z", "+02:00")[0] == "2023-07-15"
    assert date_folder("2023-07-14T22:30:00Z", "-05:00")[0] == "2023-07-14"


def test_dst_boundary():
    # Brussels is UTC+1 in winter, UTC+2 in summer.
    assert date_folder("2023-01-14T23:30:00Z", "Europe/Brussels")[0] == "2023-01-15"
    assert date_folder("2023-06-14T21:30:00Z", "Europe/Brussels")[0] == "2023-06-14"


def test_missing_or_bogus_timezone_falls_back_to_utc_with_warning():
    folder, warning = date_folder("2023-07-14T22:30:00Z", None)
    assert folder == "2023-07-14" and "UTC" in warning
    folder, warning = date_folder("2023-07-14T22:30:00Z", "Mars/Olympus")
    assert folder == "2023-07-14" and "Mars/Olympus" in warning


def test_naive_and_offset_timestamps_parse():
    assert date_folder("2023-07-14T10:00:00", "UTC")[0] == "2023-07-14"
    assert date_folder("2023-07-14T10:00:00+00:00", "UTC")[0] == "2023-07-14"


def test_empty_captured_at_is_an_error():
    with pytest.raises(CaptureDateError):
        date_folder("", "UTC")


def test_safe_filename_neutralises_traversal_and_dotfiles():
    # separators become underscores, and leading dots are stripped so a hostile
    # name cannot land as a dotfile or escape the date folder
    assert safe_filename("../../etc/passwd", "fb") == "_.._etc_passwd"
    assert safe_filename(".hidden", "fb") == "hidden"
    assert safe_filename("", "fallback") == "fallback"


def test_collision_suffix_is_derived_from_media_id():
    assert suffixed("GX010123.MP4", "abcdef123456") == "GX010123_abcdef.MP4"
    assert suffixed("noext", "abcdef123456") == "noext_abcdef"


def owners(mapping):
    return lambda path: mapping.get(path)


def test_collision_with_another_item_uses_the_stable_id_suffix():
    owned = owners({"2023-07-14/GX010001.MP4": "aaa111"})
    first = target_path("2023-07-14", "GX010001.MP4", "ddd444", owned)
    assert first == "2023-07-14/GX010001_ddd444.MP4"
    # Re-deriving the assignment later must produce the identical path,
    # otherwise resume would re-download into a new name.
    assert target_path("2023-07-14", "GX010001.MP4", "ddd444", owned) == first


def test_a_chaptered_item_colliding_with_itself_uses_the_chapter_number():
    owned = owners({"2023-07-14/GX010001.MP4": "ddd444"})
    path = target_path(
        "2023-07-14", "GX010001.MP4", "ddd444", owned, item_number=2, chapter_count=3
    )
    assert path == "2023-07-14/GX010001_p02.MP4"


def test_a_chapter_colliding_with_a_different_item_still_uses_the_id():
    # the conflict is not with a sibling chapter, so _pNN would be misleading
    owned = owners({"2023-07-14/GX010001.MP4": "aaa111"})
    path = target_path(
        "2023-07-14", "GX010001.MP4", "ddd444", owned, item_number=1, chapter_count=3
    )
    assert path == "2023-07-14/GX010001_ddd444.MP4"


def test_no_collision_means_plain_name():
    assert target_path("2023-07-14", "GX010001.MP4", "aaa", lambda p: None) == \
        "2023-07-14/GX010001.MP4"


def test_fallback_timezone_is_used_only_when_the_api_gives_none():
    """GoPro omits captured_at_timezone on essentially every item.

    Without a fallback, folder dates are UTC and clips shot late in the evening
    land in the previous day's folder.
    """
    brussels = parse_timezone("Europe/Brussels")

    # summer: UTC+2, so 22:24Z is already the next local day
    folder, warning = date_folder("2025-08-21T22:24:04Z", None, brussels)
    assert folder == "2025-08-22"
    assert "Europe/Brussels" in warning

    # winter: UTC+1, so the same clock time does NOT cross midnight
    assert date_folder("2025-01-21T22:30:00Z", None, brussels)[0] == "2025-01-21"
    assert date_folder("2025-01-21T23:30:00Z", None, brussels)[0] == "2025-01-22"

    # a timezone from the API always wins over the fallback
    assert date_folder("2025-08-21T22:24:04Z", "-05:00", brussels) == ("2025-08-21", None)


def test_fallback_defaults_to_utc_when_unset():
    assert date_folder("2025-08-21T22:24:04Z", None)[0] == "2025-08-21"


def test_parse_timezone_accepts_names_and_offsets_and_rejects_junk():
    import pytest as _pytest

    assert parse_timezone("Europe/Brussels") is not None
    assert parse_timezone("+02:00").utcoffset(None).total_seconds() == 7200
    assert parse_timezone("-0500").utcoffset(None).total_seconds() == -18000
    with _pytest.raises((ZoneInfoNotFoundError, ValueError)):
        parse_timezone("Mars/Olympus")
