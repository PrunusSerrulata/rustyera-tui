from __future__ import annotations

from rustyera_tui.client_hello import build_client_hello, captured_client_identity


def test_capture_identity_is_projection_of_production_hello() -> None:
    hello = build_client_hello(None)
    identity = captured_client_identity()
    capabilities = identity["capabilities"]

    assert len(hello[2]) == len(identity["features"])
    assert len(hello[4][0]) == len(capabilities["input_modalities"])
    assert [(service[0], service[1]) for service in hello[4][10]] == [
        (kind, operation)
        for kind, operation in (
            (9, "random_seed"),
            (8, "local_date_time"),
            (7, "device_pump"),
            (1, "image_metadata"),
            (10, "get_display_line"),
            (10, "html_get_printed_str"),
            (10, "serialize_physical_history"),
            (0, "gget_text_size"),
            (11, "rustyera.sql"),
        )
    ]
    assert [service["operation"] for service in capabilities["services"]] == [
        service[1] for service in hello[4][10]
    ]


def test_tui_identity_does_not_claim_core_only_input_capabilities() -> None:
    capabilities = captured_client_identity()["capabilities"]

    assert capabilities["input_modalities"] == ["keyboard", "mouse"]
    assert [item["name"] for item in capabilities["environment"]] == [
        "input.timed_viewport",
        "input.device_pump",
    ]
    assert all(
        service["operation"] != "get_key_state" for service in capabilities["services"]
    )
