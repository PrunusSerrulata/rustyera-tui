import pytest

import rustyera_tui.presentation as presentation_module

from presentation_test_support import line, snapshot
from rustyera_tui.presentation import (
    VIEWPORT_BUFFER_LINES,
    PresentationDeltaAccumulator,
    PresentationEventAccumulator,
    PresentationModel,
    ServicePresentationModel,
    coalesce_presentation_deltas,
    parse_line,
    plain_line,
)
from rustyera_tui.wire import variant


def test_delta_append_replace_delete_and_revision_check() -> None:
    model = PresentationModel()
    model.apply_snapshot(snapshot())
    model.apply_delta({0: 1, 1: 2, 2: [variant(0, line(2, "继续"))]})
    assert [item.line_id for item in model.lines] == [1, 2]
    model.apply_delta({0: 2, 1: 3, 2: [variant(7, 2, line(2, "载入"))]})
    assert model.lines[-1].segments[0].text == "载入"
    model.apply_delta({0: 3, 1: 4, 2: [variant(1, 1)]})
    assert [item.line_id for item in model.lines] == [1]


def test_delta_can_clear_an_optional_input_wait() -> None:
    model = PresentationModel()
    initial = snapshot()
    initial[5] = {0: 9, 1: 2, 11: {0: 1, 1: 7}}
    model.apply_snapshot(initial)

    # minicbor omits a `None` tuple field, so SetInputWait(None) has no fields.
    model.apply_delta({0: 1, 1: 2, 2: [variant(6)]})

    assert model.input_wait is None


def test_button_generation_delta_lazily_disables_every_old_button_segment() -> None:
    model = PresentationModel()
    model.apply_snapshot(snapshot())

    model.apply_delta({0: 1, 1: 2, 2: [variant(13, 1)]})

    segment = model.lines[0].segments[0]
    assert segment.generation == 0
    assert segment.enabled
    assert not model.segment_enabled(segment)


def test_partial_line_updates_keep_current_buttons_and_retire_late_old_buttons() -> None:
    model = PresentationModel()
    model.apply_snapshot(snapshot())

    model.apply_delta(
        {
            0: 1,
            1: 2,
            2: [
                variant(13, 1),
                variant(7, 1, line(1, "current map", generation=1)),
                variant(0, line(2, "late history", generation=0)),
            ],
        }
    )

    current, stale = (item.segments[0] for item in model.lines)
    assert model.segment_enabled(current)
    assert current.generation == 1
    assert not model.segment_enabled(stale)
    assert stale.generation == 0


def test_snapshot_forgets_local_button_generation_before_later_partial_updates() -> None:
    model = PresentationModel()
    model.apply_snapshot(snapshot())
    model.apply_delta({0: 1, 1: 2, 2: [variant(13, 1)]})

    resynchronized = snapshot()
    resynchronized[0] = 3
    resynchronized[2] = {0: [line(1, "snapshot", generation=2)], 1: []}
    model.apply_snapshot(resynchronized)
    model.apply_delta(
        {
            0: 3,
            1: 4,
            2: [
                variant(7, 1, line(1, "replacement", generation=2)),
                variant(0, line(2, "append", generation=2)),
            ],
        }
    )

    assert all(segment.enabled for item in model.lines for segment in item.segments)


def test_submitted_buttons_retire_while_later_partial_updates_stay_enabled() -> None:
    model = PresentationModel()
    model.apply_snapshot(snapshot())

    retired = model.retire_presented_interactions()
    model.apply_delta({0: 1, 1: 2, 2: [variant(0, line(2, "dynamic map"))]})

    assert not model.segment_enabled(model.lines[0].segments[0])
    assert model.segment_enabled(model.lines[1].segments[0])

    resynchronized = snapshot()
    resynchronized[0] = 3
    model.apply_snapshot(resynchronized)
    assert not model.segment_enabled(model.lines[0].segments[0])

    model.restore_interaction_boundary(retired)
    assert model.segment_enabled(model.lines[0].segments[0])


def test_unchanged_submitted_buttons_rearm_without_reviving_replacements() -> None:
    model = PresentationModel()
    model.apply_snapshot(snapshot())

    unchanged_boundary = model.retire_presented_interactions()
    assert model.restore_submitted_interaction_boundary(unchanged_boundary)
    assert model.segment_enabled(model.lines[0].segments[0])

    replaced_boundary = model.retire_presented_interactions()
    model.apply_delta({0: 1, 1: 2, 2: [variant(0, line(2, "dynamic map"))]})
    assert not model.restore_submitted_interaction_boundary(replaced_boundary)
    assert not model.segment_enabled(model.lines[0].segments[0])
    assert model.segment_enabled(model.lines[1].segments[0])

    current_boundary = model.retire_presented_interactions()
    model.apply_delta({0: 2, 1: 3, 2: [variant(13, 1)]})
    assert model.restore_submitted_interaction_boundary(current_boundary)
    assert not model.segment_enabled(model.lines[1].segments[0])


def test_button_policy_changes_do_not_rewrite_accumulated_history() -> None:
    model = PresentationModel()
    initial = snapshot()
    initial[2] = {0: [line(index, f"line {index}") for index in range(1, 5_001)], 1: []}
    initial[6][4] = 5_000
    model.apply_snapshot(initial)
    before = tuple(id(item) for item in model.lines)
    model.take_render_change()

    retired = model.retire_presented_interactions()
    model.apply_delta({0: 1, 1: 2, 2: [variant(13, 1)]})

    assert tuple(id(item) for item in model.lines) == before
    assert model.take_render_change() == (len(model.lines), 0)
    assert not model.segment_enabled(model.lines[-1].segments[0])

    model.apply_delta({0: 2, 1: 3, 2: [variant(0, line(5_001, "new", generation=1))]})
    assert model.segment_enabled(model.lines[-1].segments[0])
    model.restore_interaction_boundary(retired)
    assert not model.segment_enabled(model.lines[0].segments[0])


def test_line_index_stays_valid_across_replace_delete_and_append() -> None:
    model = PresentationModel()
    model.apply_snapshot(snapshot())
    model.apply_delta(
        {
            0: 1,
            1: 2,
            2: [variant(0, line(2, "two")), variant(0, line(3, "three"))],
        }
    )
    model.apply_delta(
        {
            0: 2,
            1: 3,
            2: [variant(1, 2), variant(0, line(4, "four")), variant(7, 1, line(1, "one"))],
        }
    )

    assert [item.line_id for item in model.lines] == [1, 4]
    assert model.lines[0].segments[0].text == "one"
    assert model._line_indices == {1: 0, 4: 1}


def test_trim_lines_removes_the_oldest_history_and_reports_incremental_hints() -> None:
    rich = PresentationModel()
    service = ServicePresentationModel()
    initial = snapshot()
    initial[2][0] = [line(1, "one"), line(2, "two"), line(3, "three")]
    rich.apply_snapshot(initial)
    service.apply_snapshot(initial)
    rich.take_render_change()
    delta = {
        0: 1,
        1: 2,
        2: [variant(14, 1), variant(0, line(4, "four"))],
    }

    rich.apply_delta(delta)
    service.apply_delta(delta)
    replacement = {
        0: 2,
        1: 3,
        2: [variant(7, 2, line(2, "TWO"))],
    }
    rich.apply_delta(replacement)
    service.apply_delta(replacement)

    assert [item.line_id for item in rich.lines] == [2, 3, 4]
    assert [item.line_id for item in service.lines] == [2, 3, 4]
    assert rich.lines[0].segments[0].text == "TWO"
    assert plain_line(service.lines[0]) == "TWO"
    # The replacement targets the first retained row after the prefix trim. The trim hint lets
    # the viewport discard the old prefix, while changed_from remains relative to the new list.
    assert rich.take_render_change() == (0, 1)


def test_main_viewport_tracks_the_runtime_max_log_setting() -> None:
    model = PresentationModel()
    initial = snapshot()
    configured_limit = 1_500
    initial[2][0] = [
        {0: line_id, 1: False, 2: True, 3: True, 4: 0, 5: []}
        for line_id in range(1, configured_limit + 3)
    ]
    initial[6][4] = configured_limit

    model.apply_snapshot(initial)

    assert len(model.lines) == configured_limit
    assert [item.line_id for item in model.lines[:2]] == [3, 4]
    assert model.lines[-1].segments == ()
    assert model.maximum_physical_lines == configured_limit

    model.take_render_change()
    model.apply_delta({0: 1, 1: 2, 2: [variant(14, 2)]})
    assert len(model.lines) == configured_limit
    assert model.lines[0].line_id == 3
    assert model.take_render_change() == (None, 0)


def test_snapshot_parses_only_the_retained_viewport_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = PresentationModel()
    initial = snapshot()
    configured_limit = 500
    initial[2][0] = [line(line_id, str(line_id)) for line_id in range(1, 701)]
    initial[6][4] = configured_limit
    parsed: list[int] = []
    original_parse_line = presentation_module.parse_line

    def tracked_parse_line(raw: dict[int, object]):
        parsed.append(int(raw[0]))
        return original_parse_line(raw)

    monkeypatch.setattr(presentation_module, "parse_line", tracked_parse_line)

    model.apply_snapshot(initial)

    assert parsed == list(range(201, 701))
    assert [item.line_id for item in model.lines] == parsed
    model.take_render_change()
    model.apply_delta({0: 1, 1: 2, 2: [variant(14, 200)]})
    assert [item.line_id for item in model.lines] == parsed
    assert model.take_render_change() == (None, 0)


def test_viewport_limit_trims_incremental_blank_lines_with_render_hints() -> None:
    model = PresentationModel()
    initial = snapshot()
    initial[2][0] = [
        {0: line_id, 1: False, 2: True, 3: True, 4: 0, 5: []}
        for line_id in range(1, VIEWPORT_BUFFER_LINES + 1)
    ]
    model.apply_snapshot(initial)
    model.take_render_change()

    model.apply_delta(
        {
            0: 1,
            1: 2,
            2: [
                variant(
                    0,
                    {
                        0: VIEWPORT_BUFFER_LINES + 1,
                        1: False,
                        2: True,
                        3: True,
                        4: 0,
                        5: [],
                    },
                )
            ],
        }
    )

    assert len(model.lines) == VIEWPORT_BUFFER_LINES
    assert model.lines[0].line_id == 2
    assert model.lines[-1].segments == ()
    assert model.take_render_change() == (VIEWPORT_BUFFER_LINES - 1, 1)


def test_raw_worker_projection_tracks_rich_text_and_wait_state() -> None:
    rich = PresentationModel()
    service = ServicePresentationModel()
    initial = snapshot()
    rich.apply_snapshot(initial)
    service.apply_snapshot(initial)
    delta = {
        0: 1,
        1: 2,
        2: [variant(7, 1, line(1, "loaded")), variant(6, {0: 2, 1: 2})],
    }
    rich.apply_delta(delta)
    service.apply_delta(delta)

    assert [plain_line(line) for line in service.lines] == [
        "".join(segment.text for segment in rich.lines[0].segments)
    ]
    assert service.input_wait == rich.input_wait == {0: 2, 1: 2}


def test_service_projection_compacts_lines_and_suppresses_retired_history_until_clear() -> None:
    service = ServicePresentationModel()
    initial = snapshot()
    service.apply_snapshot(initial)

    assert service.lines[0].text == "开始"
    assert not isinstance(service.lines[0], dict)
    with pytest.raises(ValueError, match="service limit"):
        service.html_printed_str(0, 8)

    retired_revision = service.retire_history()
    stale = service.apply_delta({0: retired_revision, 1: 2, 2: [variant(0, line(2, "stale"))]})
    assert stale[2] == []
    assert service.lines == []

    replacement = service.apply_delta(
        {
            0: 2,
            1: 3,
            2: [variant(2), variant(0, line(3, "title"))],
        }
    )
    assert replacement[2]
    assert [item.text for item in service.lines] == ["title"]


def test_viewport_snapshot_enforces_line_byte_and_segment_hard_budgets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(presentation_module, "MAXIMUM_VIEWPORT_UTF8_BYTES", 8)
    monkeypatch.setattr(presentation_module, "MAXIMUM_VIEWPORT_SEGMENTS", 2)
    model = PresentationModel()
    initial = snapshot()
    initial[6][4] = 1_000_000
    initial[2][0] = [
        {
            0: line_id,
            1: False,
            2: True,
            3: True,
            4: 0,
            5: [variant(0, text, None, None)],
        }
        for line_id, text in ((1, "aaaaa"), (2, "bbbbb"), (3, "cc"))
    ]

    model.apply_snapshot(initial)

    assert model.maximum_physical_lines == presentation_module.MAXIMUM_VIEWPORT_BUFFER_LINES
    assert [item.line_id for item in model.lines] == [2, 3]
    assert model._retained_utf8_bytes <= presentation_module.MAXIMUM_VIEWPORT_UTF8_BYTES
    assert model._retained_segments <= presentation_module.MAXIMUM_VIEWPORT_SEGMENTS


def test_display_line_cost_includes_button_titles_and_separator_patterns() -> None:
    button_line = parse_line(line(1, "x"))
    separator_line = parse_line({0: 2, 1: False, 2: True, 3: True, 4: 0, 5: [variant(6, "～", 0)]})

    assert button_line._retained_utf8_bytes == len("x选择".encode())
    assert button_line._retained_segments == 1
    assert separator_line._retained_utf8_bytes == len("～".encode())
    assert separator_line._retained_segments == 1


def test_rejected_replacement_stays_retired_until_resync_snapshot() -> None:
    service = ServicePresentationModel()
    service.apply_snapshot(snapshot())
    revision = service.begin_replacement(41)

    assert service.reject_replacement(41)
    filtered = service.apply_delta(
        {0: revision, 1: revision + 1, 2: [variant(0, line(2, "stale"))]}
    )
    assert filtered[2] == []
    assert service.lines == []

    replacement = snapshot()
    replacement[0] = revision + 1
    replacement[2] = {0: [line(3, "resynchronized")], 1: []}
    service.apply_snapshot(replacement)
    service.apply_delta({0: revision + 1, 1: revision + 2, 2: [variant(0, line(4, "new"))]})
    assert [item.text for item in service.lines] == ["resynchronized", "new"]


def test_rich_replacement_boundary_clears_on_clear_history() -> None:
    model = PresentationModel()
    model.apply_snapshot(snapshot())
    model.retire_history(model.revision)

    model.apply_delta({0: 1, 1: 2, 2: [variant(0, line(2, "stale"))]})
    assert model.lines == []
    model.apply_delta(
        {
            0: 2,
            1: 3,
            2: [variant(2), variant(0, line(3, "replacement"))],
        }
    )

    assert [item.line_id for item in model.lines] == [3]


def test_delta_coalescing_preserves_state_and_discards_superseded_lines() -> None:
    original = [
        {
            0: 1,
            1: 2,
            2: [variant(0, line(2, "draft")), variant(7, 1, line(1, "old"))],
        },
        {
            0: 2,
            1: 3,
            2: [
                variant(7, 2, line(2, "final")),
                variant(7, 1, line(1, "new")),
                variant(6, {0: 4}),
                variant(6),
            ],
        },
    ]
    sequential = PresentationModel()
    sequential.apply_snapshot(snapshot())
    for delta in original:
        sequential.apply_delta(delta)
    combined = PresentationModel()
    combined.apply_snapshot(snapshot())
    coalesced = coalesce_presentation_deltas(original)
    combined.apply_delta(coalesced)

    assert combined == sequential
    assert len(coalesced[2]) < sum(len(delta[2]) for delta in original)


def test_delta_coalescing_preserves_button_generation_order() -> None:
    original = [
        {
            0: 1,
            1: 2,
            2: [variant(13, 1), variant(0, line(2, "one", generation=1))],
        },
        {
            0: 2,
            1: 3,
            2: [variant(13, 2), variant(0, line(3, "two", generation=2))],
        },
    ]
    sequential = PresentationModel()
    sequential.apply_snapshot(snapshot())
    for delta in original:
        sequential.apply_delta(delta)
    combined = PresentationModel()
    combined.apply_snapshot(snapshot())
    combined.apply_delta(coalesce_presentation_deltas(original))

    assert combined == sequential
    assert [
        combined.segment_enabled(segment) for item in combined.lines for segment in item.segments
    ] == [
        False,
        False,
        True,
    ]


def test_incremental_delta_accumulator_crosses_two_former_compaction_windows_once() -> None:
    deltas = [
        {
            0: revision,
            1: revision + 1,
            2: [
                variant(0, line(2, "value 0"))
                if revision == 1
                else variant(7, 2, line(2, f"value {revision}"))
            ],
        }
        for revision in range(1, 131)
    ]
    accumulator = PresentationDeltaAccumulator()
    for delta in deltas:
        accumulator.add(delta)

    combined_delta = accumulator.take()
    assert combined_delta is not None
    assert len(combined_delta[2]) == 1
    assert accumulator.take() is None

    sequential = PresentationModel()
    sequential.apply_snapshot(snapshot())
    for delta in deltas:
        sequential.apply_delta(delta)
    accumulated = PresentationModel()
    accumulated.apply_snapshot(snapshot())
    accumulated.apply_delta(combined_delta)
    assert accumulated == sequential


def test_presentation_event_accumulator_snapshot_discards_older_delta_state() -> None:
    accumulator = PresentationEventAccumulator()
    accumulator.add_delta({0: 1, 1: 2, 2: [variant(0, line(2, "discarded"))]})
    replacement = snapshot()
    replacement[0] = 20
    accumulator.replace_snapshot(replacement)
    following = {0: 20, 1: 21, 2: [variant(0, line(3, "retained"))]}
    accumulator.add_delta(following)

    assert accumulator.take() == (replacement, following)
    assert accumulator.take() == (None, None)


def test_incremental_accumulator_matches_destructive_and_stateful_delta_sequence() -> None:
    updated_settings = dict(snapshot()[6])
    updated_settings[0] = 500_000
    original = [
        {
            0: 1,
            1: 2,
            2: [
                variant(0, line(2, "two")),
                variant(3, "first title"),
                variant(8, updated_settings),
            ],
        },
        {
            0: 2,
            1: 3,
            2: [
                variant(7, 2, line(2, "replacement")),
                variant(1, 1),
                variant(0, line(3, "three", generation=1)),
                variant(13, 1),
                variant(6, {0: 5, 1: 2}),
            ],
        },
        {
            0: 3,
            1: 4,
            2: [
                variant(2),
                variant(0, line(4, "after clear", generation=1)),
                variant(14, 1),
                variant(3, "final title"),
                variant(6),
            ],
        },
    ]
    accumulator = PresentationDeltaAccumulator()
    for delta in original:
        accumulator.add(delta)
    combined_delta = accumulator.take()
    assert combined_delta is not None

    sequential = PresentationModel()
    sequential.apply_snapshot(snapshot())
    for delta in original:
        sequential.apply_delta(delta)
    accumulated = PresentationModel()
    accumulated.apply_snapshot(snapshot())
    accumulated.apply_delta(combined_delta)

    assert accumulated == sequential
