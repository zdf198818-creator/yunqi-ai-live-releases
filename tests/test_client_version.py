from ailive import __version__
from ailive.client.update_service import APP_VERSION


def test_packaged_client_version_matches_current_release() -> None:
    assert APP_VERSION == __version__ == "0.9.12"
