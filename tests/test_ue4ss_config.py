from backend.ue4ss_config import (
    enabled_state,
    merge_missing_entries,
    parse_entry,
    remove_entries,
    update_entry,
)


def test_update_entry_preserves_bom_comments_newlines_and_removes_duplicates():
    original = b"\xef\xbb\xbf; header\r\nCoolMod : 0 # old\r\nCOOLMOD : 1\r\nOther : 1\r\n"

    updated = update_entry(original, "coolmod", True)

    assert updated == b"\xef\xbb\xbf; header\r\nCoolMod : 1 # old\r\nOther : 1\r\n"
    assert enabled_state(updated, "CoolMod") is True


def test_update_entry_appends_with_existing_newline_style():
    assert update_entry(b"Other : 1\r\n", "NewMod", False) == (
        b"Other : 1\r\nNewMod : 0\r\n"
    )
    assert update_entry(b"; no newline", "NewMod", True) == (
        b"; no newline\nNewMod : 1\n"
    )


def test_remove_entries_only_removes_exact_framework_names():
    original = b"BPModLoaderMod : 1\nUserBPModLoaderMod : 1\nKeybinds : 1 ; built in\n"

    assert remove_entries(original, {"bpmodloadermod", "keybinds"}) == (
        b"UserBPModLoaderMod : 1\n"
    )


def test_merge_missing_entries_preserves_current_values_and_copies_bundled_defaults():
    current = b"; user\r\nBPModLoaderMod : 0\r\nUserMod : 1\r\n"
    bundled = b"BPModLoaderMod : 1\nKeybinds : 1 ; default\nConsoleCommandsMod : 0\n"

    merged = merge_missing_entries(
        current,
        bundled,
        {"bpmodloadermod", "keybinds", "consolecommandsmod"},
    )

    assert merged == (
        b"; user\r\nBPModLoaderMod : 0\r\nUserMod : 1\r\n"
        b"Keybinds : 1\r\nConsoleCommandsMod : 0\r\n"
    )


def test_parse_entry_accepts_bom_whitespace_and_comment_but_rejects_other_lines():
    assert parse_entry("\ufeff Cool : 1 # enabled") == ("Cool", "1")
    assert parse_entry("; comment") is None
    assert parse_entry("Cool = 1") is None
