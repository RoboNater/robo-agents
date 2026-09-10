from agent_hub_common import MetaKeys


def test_meta_keys_are_prefixed_and_unique() -> None:
    values = [key.value for key in MetaKeys]
    assert len(values) == len(set(values))
    for key in MetaKeys:
        assert key.startswith("hub.")
        assert isinstance(key, str)
