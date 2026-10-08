import pytest

from gtracker.compat import git_version_error


@pytest.mark.parametrize(
    "output",
    [
        "git version 2.45.0",
        "git version 2.46.0",
        "git version 2.100.0",
        "git version 3.0.0",
        "git version 2.45.0.windows.1",
        "git version 2.45.0 (Apple Git-154)",
        "git version 2.45.0 (Debian 1:2.45.0-1)",
        "git version 2.45.0.123.gabcdef",
        "git version 2.46.0.rc1",
    ],
)
def test_supported_git_versions_and_vendor_suffixes(output: str) -> None:
    assert git_version_error(output) is None


@pytest.mark.parametrize(
    "output",
    [
        "git version 2.9.9",
        "git version 2.44.9",
        "git version 2.45.0-rc1",
    ],
)
def test_older_git_versions_explain_required_upgrade(output: str) -> None:
    error = git_version_error(output)
    assert error is not None
    assert "Git 2.45.0 or newer is required" in error
    assert "GIT_NO_LAZY_FETCH" in error
    assert output in error


@pytest.mark.parametrize(
    "output",
    [
        "",
        "2.45.0",
        "git version 2.45.0garbage",
        "git version ٢.٤٥.٠",
    ],
)
def test_malformed_git_version_output_is_diagnostic(output: str) -> None:
    error = git_version_error(output)
    assert error is not None
    assert "Cannot parse Git version" in error
    assert "Git 2.45.0 or newer is required" in error
    assert repr(output.strip()) in error
