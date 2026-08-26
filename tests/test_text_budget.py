from rustyera_tui.text_budget import bounded_repr, truncate_utf8, utf8_length


def test_utf8_budget_helpers_bound_multibyte_text_without_splitting_characters() -> None:
    text = "甲" * 5_000

    assert utf8_length(text) == len(text.encode())
    truncated = truncate_utf8(text, 10)
    assert len(truncated.encode()) <= 10
    assert truncated.endswith("…")


def test_bounded_repr_limits_strings_and_nested_objects() -> None:
    rendered_string = bounded_repr("内容" * 10_000, 128)
    rendered_object = bounded_repr({"values": list(range(10_000))}, 128)

    assert len(rendered_string.encode()) <= 128
    assert rendered_string.startswith("'")
    assert len(rendered_object.encode()) <= 128
    assert "..." in rendered_object
