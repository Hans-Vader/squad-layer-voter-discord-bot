import bot as botmod


def test_validate_duration_str_empty_is_none():
    assert botmod.validate_duration_str("") == (True, None)
    assert botmod.validate_duration_str("   ") == (True, None)
    assert botmod.validate_duration_str(None) == (True, None)


def test_validate_duration_str_valid_keeps_trimmed_string():
    assert botmod.validate_duration_str(" 1h ") == (True, "1h")
    assert botmod.validate_duration_str("30m") == (True, "30m")
    assert botmod.validate_duration_str("2d") == (True, "2d")


def test_validate_duration_str_invalid():
    assert botmod.validate_duration_str("abc") == (False, None)
    assert botmod.validate_duration_str("-5") == (False, None)


def test_format_duration_str():
    assert botmod._format_property_value("1h", "duration_str") == "1h"
    assert botmod._format_property_value(None, "duration_str") == "—"
    assert botmod._format_property_value("", "duration_str") == "—"
