from rustyera_tui.presentation import PresentationModel
from rustyera_tui.wire import variant


def color(red: int, green: int, blue: int) -> dict[int, int]:
    return {0: red, 1: green, 2: blue, 3: 255}


def style(foreground: dict[int, int]) -> dict[int, object]:
    return {0: foreground, 2: False, 3: False, 4: False, 5: False, 7: 12_000}


def line(line_id: int, text: str) -> dict[int, object]:
    button = variant(
        1,
        [variant(0, text, style(color(0, 255, 128)), None)],
        {0: 1, 1: line_id},
        "选择",
        None,
        variant(0, line_id),
        0,
        True,
    )
    return {0: line_id, 1: False, 2: True, 3: True, 4: 0, 5: [button]}


def snapshot() -> dict[int, object]:
    settings = {
        0: 100_000,
        1: 1_000,
        2: color(0, 0, 0),
        3: color(255, 255, 255),
        4: 1000,
        5: True,
        6: False,
    }
    return {
        0: 1,
        1: "Game",
        2: {0: [line(1, "开始")], 1: []},
        5: None,
        6: settings,
    }


def test_snapshot_preserves_color_and_button_token() -> None:
    model = PresentationModel()
    model.apply_snapshot(snapshot())
    assert model.title == "Game"
    assert model.lines[0].logical_line_start
    assert model.lines[0].segments[0].text == "开始"
    assert model.lines[0].segments[0].style.foreground == "#00ff80"
    assert model.lines[0].segments[0].token == {0: 1, 1: 1}


def test_delta_append_replace_delete_and_revision_check() -> None:
    model = PresentationModel()
    model.apply_snapshot(snapshot())
    model.apply_delta({0: 1, 1: 2, 2: [variant(0, line(2, "继续"))]})
    assert [item.line_id for item in model.lines] == [1, 2]
    model.apply_delta({0: 2, 1: 3, 2: [variant(7, 2, line(2, "载入"))]})
    assert model.lines[-1].segments[0].text == "载入"
    model.apply_delta({0: 3, 1: 4, 2: [variant(1, 1)]})
    assert [item.line_id for item in model.lines] == [1]
