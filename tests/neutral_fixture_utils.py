"""Builders for reserved synthetic locators without tracked complete URLs."""

from __future__ import annotations


def neutral_https_locator(
    host_components: tuple[str, ...],
    suffix_components: tuple[str, ...],
    path_components: tuple[str, ...] = (),
) -> str:
    """Assemble one HTTPS fixture locator from separately stored components."""

    host = "-".join(host_components)
    suffix = "".join(suffix_components)
    if not host or suffix not in {"invalid", "test"}:
        raise ValueError("fixture locator components are invalid")
    if any(not part or not part.replace("-", "").isalnum() for part in host_components):
        raise ValueError("fixture host components are invalid")
    if any(not part or "/" in part for part in path_components):
        raise ValueError("fixture path components are invalid")
    path = "/" + "/".join(path_components) if path_components else ""
    return "https" + "://" + host + "." + suffix + path


def neutral_locator_from_spec(spec: dict[str, object]) -> str:
    """Build one reserved locator from a JSON fixture component object."""

    if set(spec) != {"host_components", "suffix_components", "path_components"}:
        raise ValueError("fixture locator specification is invalid")
    values = tuple(spec[name] for name in (
        "host_components", "suffix_components", "path_components",
    ))
    if any(type(value) is not list or any(type(item) is not str for item in value)
           for value in values):
        raise ValueError("fixture locator specification is invalid")
    return neutral_https_locator(*(tuple(value) for value in values))


def neutral_public_https_locator(
    host_components: tuple[str, ...],
    path_components: tuple[str, ...] = (),
) -> str:
    """Assemble a non-reserved synthetic public locator for fake transports."""

    if any(not part or not part.replace("-", "").isalnum() for part in host_components):
        raise ValueError("fixture host components are invalid")
    if any(not part or "/" in part for part in path_components):
        raise ValueError("fixture path components are invalid")
    host = "-".join(host_components) + "." + "example" + "." + "com"
    path = "/" + "/".join(path_components) if path_components else ""
    return "https" + "://" + host + path
