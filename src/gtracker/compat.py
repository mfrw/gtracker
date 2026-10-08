"""Git capabilities required for safe, machine-readable repository operations."""

import re

# Git 2.45 introduced GIT_NO_LAZY_FETCH; fetch --porcelain arrived in 2.41.
# https://github.com/git/git/blob/v2.45.0/Documentation/RelNotes/2.45.0.txt
MIN_GIT_VERSION = (2, 45, 0)


def git_version_error(output: str) -> str | None:
    """Return an actionable diagnostic for unsupported or unrecognized Git."""
    text = output.strip()
    required = ".".join(map(str, MIN_GIT_VERSION))
    requirement = f"Git {required} or newer is required"
    match = re.fullmatch(
        r"git version ([0-9]+)\.([0-9]+)\.([0-9]+)"
        r"(?P<prerelease>[.-]rc[0-9]+)?"
        r"(?:[.+-][0-9A-Za-z][0-9A-Za-z.+-]*)?"
        r"(?: \([0-9A-Za-z][0-9A-Za-z .:+_/-]*\))?",
        text,
    )
    if match is None:
        return f"Cannot parse Git version from {text!r}; {requirement}."
    version = tuple(int(part) for part in match.group(1, 2, 3))
    if version < MIN_GIT_VERSION or (
        version == MIN_GIT_VERSION and match.group("prerelease") is not None
    ):
        return (
            f"{requirement} for GIT_NO_LAZY_FETCH and machine-readable fetch results; "
            f"found {text}. Upgrade Git before running gtracker."
        )
    return None
