"""Smoke checks for the initial package skeleton."""


def test_package_imports_without_network() -> None:
    """The package metadata is importable without constructing a network client."""

    import enbek_monitor

    assert enbek_monitor.__version__ == "0.1.0"
