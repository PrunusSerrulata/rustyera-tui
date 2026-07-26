from rich.text import Text

from rustyera_tui.log_model import (
    LogLevel,
    filter_log_entries,
    format_log_entries,
    make_log_entry,
)
from rustyera_tui.runtime import diagnostic_log_level, log_event


def test_log_entry_normalizes_repeated_severity_prefixes_and_aligns_levels() -> None:
    entry = make_log_entry(
        "ERROR: [Warning] ERROR disk unavailable",
        LogLevel.WARNING,
        timestamp="12:34:56",
    )

    assert entry.level is LogLevel.ERROR
    assert entry.message == "disk unavailable"
    assert entry.plain_text == "[12:34:56] ERROR disk unavailable"

    info = make_log_entry("ready", LogLevel.INFO, timestamp="12:34:57")
    assert info.plain_text == "[12:34:57] INFO  ready"


def test_log_entry_renders_colored_bold_time_and_level() -> None:
    expected_styles = {
        LogLevel.ERROR: "bold red",
        LogLevel.WARNING: "bold #ffbf00",
        LogLevel.INFO: "bold white",
        LogLevel.DEBUG: "bold grey70",
    }

    for level, expected_style in expected_styles.items():
        rendered = make_log_entry("message", level, timestamp="01:02:03").render()
        assert isinstance(rendered, Text)
        assert rendered.plain.startswith(f"[01:02:03] {level.label}")
        assert str(rendered.spans[0].style) == "bold green"
        assert str(rendered.spans[1].style) == expected_style


def test_log_filter_is_threshold_only_but_export_always_contains_every_entry() -> None:
    entries = [
        make_log_entry("debug", LogLevel.DEBUG, timestamp="00:00:01"),
        make_log_entry("info", LogLevel.INFO, timestamp="00:00:02"),
        make_log_entry("warning", LogLevel.WARNING, timestamp="00:00:03"),
        make_log_entry("error", LogLevel.ERROR, timestamp="00:00:04"),
    ]

    assert [entry.message for entry in filter_log_entries(entries, LogLevel.WARNING)] == [
        "warning",
        "error",
    ]
    assert format_log_entries(entries) == (
        "[00:00:01] DEBUG debug\n"
        "[00:00:02] INFO  info\n"
        "[00:00:03] WARN  warning\n"
        "[00:00:04] ERROR error\n"
    )


def test_runtime_diagnostic_severity_is_preserved_as_structured_log_data() -> None:
    assert diagnostic_log_level(0) is LogLevel.INFO
    assert diagnostic_log_level(1) is LogLevel.WARNING
    assert diagnostic_log_level(2) is LogLevel.ERROR

    event = log_event("[runtime.example]: failed", LogLevel.ERROR)
    assert event.kind == "log"
    assert event.value.level is LogLevel.ERROR
    assert event.value.message == "[runtime.example]: failed"
