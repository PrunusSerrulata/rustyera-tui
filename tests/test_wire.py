from rustyera_tui.wire import (
    CHANNEL_RUNTIME,
    RUNTIME_VERSION,
    decode_envelope,
    encode,
    encode_envelope,
    message_value,
    runtime_message,
    variant,
    version,
)


def test_protocol_version_matches_rust_canonical_fixture() -> None:
    assert encode(version(1, 0)) == bytes.fromhex("a200010100")


def test_minicbor_enum_shape_is_explicit() -> None:
    assert encode(variant(2, "text")) == bytes.fromhex("8202816474657874")
    assert message_value(runtime_message(30, {0: 7}), 30) == {0: 7}


def test_envelope_round_trip_preserves_numeric_contract() -> None:
    session = {0: 11, 1: 22}
    encoded = encode_envelope(
        channel=CHANNEL_RUNTIME,
        channel_version=RUNTIME_VERSION,
        session=session,
        sequence=3,
        message_id=4,
        correlation_id=2,
        payload_tag=30,
        payload=runtime_message(30, {0: 9}),
        epoch=5,
    )
    decoded = decode_envelope(encoded)
    assert decoded.channel == CHANNEL_RUNTIME
    assert decoded.session == session
    assert decoded.sequence == 3
    assert decoded.message_id == 4
    assert decoded.correlation_id == 2
    assert decoded.epoch == 5
    assert message_value(decoded.payload, decoded.payload_tag) == {0: 9}
