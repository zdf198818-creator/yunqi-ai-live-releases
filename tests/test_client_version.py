from ailive.client.update_service import APP_VERSION


def test_packaged_client_version_matches_current_release() -> None:
    assert APP_VERSION == "0.9.12"
