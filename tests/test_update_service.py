from ailive.client.update_service import is_newer_version, version_key


def test_version_key_handles_v_prefix_and_suffix() -> None:
    assert version_key("v1.2.10-beta") == (1, 2, 10)


def test_newer_version_comparison() -> None:
    assert is_newer_version("0.9.4", "0.9.3")
    assert not is_newer_version("0.9.3", "0.9.3")
    assert not is_newer_version("0.9.2", "0.9.3")
