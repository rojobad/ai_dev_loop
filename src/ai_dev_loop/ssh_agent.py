"""Infrastructure-neutral OpenSSH IdentityAgent / SSH_AUTH_SOCK resolution.

Shared by legacy publication preflight and pr-review-v2 Git transport. Pure URL
parsing, ``ssh -G`` IdentityAgent extraction, and socket selection live here.
Callers own process execution (injected runners or ``run_process``) so v2 never
imports legacy PR-review command machinery.

Errors never embed socket paths, ``ssh -G`` output, key fingerprints, or
credentials.
"""

from __future__ import annotations

import stat
from pathlib import Path

from ai_dev_loop.errors import SshAgentNoIdentityError, ValidationError

_NO_IDENTITY_MESSAGE = "ssh-agent has no usable keys; preload the SSH key before publication"


def token_looks_like_option(token: str) -> bool:
    return token.startswith("-")


def destination_has_unsafe_characters(destination: str) -> bool:
    if not destination:
        return True
    for char in destination:
        if char in {"\0", "\n", "\r", " ", "\t", "\f", "\v"}:
            return True
        if ord(char) < 32:
            return True
    return False


def validate_ssh_destination(destination: str) -> str:
    """Return a single argv-safe ssh destination, or raise ValidationError."""

    if destination_has_unsafe_characters(destination):
        raise ValidationError("SSH remote destination is empty or contains unsafe characters")
    if token_looks_like_option(destination):
        raise ValidationError("SSH remote destination looks like an option")
    if "@" in destination:
        _user, host = destination.rsplit("@", 1)
        if token_looks_like_option(host):
            raise ValidationError("SSH remote destination looks like an option")
    return destination


def ssh_destination_from_scp_url(url: str) -> str:
    # git@alias:owner/repo.git — preserve the Host alias, not a resolved hostname.
    rest = url[len("git@") :]
    if ":" not in rest:
        raise ValidationError("malformed SCP SSH remote URL")
    host, path = rest.split(":", 1)
    if not host or not path or path.startswith("/"):
        raise ValidationError("malformed SCP SSH remote URL")
    if "/" in host or "@" in host:
        raise ValidationError("malformed SCP SSH remote URL")
    if token_looks_like_option(host):
        raise ValidationError("SSH remote destination looks like an option")
    return validate_ssh_destination(f"git@{host}")


def ssh_destination_from_ssh_url(url: str) -> str:
    # ssh://user@alias[:port]/owner/repo.git — parse authority without lowercasing.
    rest = url[len("ssh://") :]
    if "/" not in rest:
        raise ValidationError("malformed SSH remote URL")
    authority, path = rest.split("/", 1)
    if not authority or not path:
        raise ValidationError("malformed SSH remote URL")
    if authority.startswith("[") or token_looks_like_option(authority):
        raise ValidationError("unsupported SSH remote URL authority")

    user: str | None
    hostport: str
    if "@" in authority:
        user, hostport = authority.rsplit("@", 1)
        if not user or not hostport or "@" in user:
            raise ValidationError("malformed SSH remote URL")
        if token_looks_like_option(user):
            raise ValidationError("SSH remote destination looks like an option")
    else:
        user = None
        hostport = authority

    if ":" in hostport:
        host, port = hostport.rsplit(":", 1)
        if not host or not port.isdigit():
            raise ValidationError("malformed SSH remote URL")
    else:
        host = hostport
    if not host or "/" in host:
        raise ValidationError("malformed SSH remote URL")
    if token_looks_like_option(host):
        raise ValidationError("SSH remote destination looks like an option")

    destination = f"{user}@{host}" if user else host
    return validate_ssh_destination(destination)


def ssh_destination_from_remote_url(url: str) -> str:
    """Build the single ``ssh -G`` destination for a supported Git SSH remote."""

    if url.startswith("https://") or url.startswith("http://"):
        raise ValidationError(
            "GitHub publication requires an SSH remote URL; "
            "configure the remote for SSH and preload ssh-agent"
        )
    if url.startswith("git@"):
        return ssh_destination_from_scp_url(url)
    if url.startswith("ssh://"):
        return ssh_destination_from_ssh_url(url)
    raise ValidationError(f"unsupported remote URL scheme for publication: {url[:32]}")


def is_usable_agent_socket(path: Path) -> bool:
    if not path.is_absolute():
        return False
    try:
        mode = path.stat().st_mode
    except OSError:
        return False
    return stat.S_ISSOCK(mode)


def parse_identity_agent_from_ssh_g(stdout: str) -> str | None:
    """Return a validated IdentityAgent socket path, or None for absent/none.

    Ambiguous, relative, missing, or non-socket values raise ValidationError.
    Does not embed ``ssh -G`` output or socket paths in the error message.
    """

    values: list[str] = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        key, separator, remainder = line.partition(" ")
        if key.lower() != "identityagent":
            continue
        if not separator:
            values.append("")
            continue
        values.append(remainder.strip())

    if not values:
        return None
    if len(values) > 1:
        raise ValidationError("ambiguous IdentityAgent in effective SSH configuration")

    value = values[0]
    if not value or value.lower() == "none":
        return None

    path = Path(value)
    if not path.is_absolute():
        raise ValidationError("effective IdentityAgent must be an absolute socket path")
    if not is_usable_agent_socket(path):
        raise ValidationError("effective IdentityAgent is not a usable SSH agent socket")
    return str(path)


def choose_effective_ssh_auth_sock(
    *,
    identity_agent: str | None,
    inherited_ssh_auth_sock: str | None,
) -> str:
    """Select IdentityAgent over inherited ``SSH_AUTH_SOCK``.

    A validated effective IdentityAgent wins. When it is absent or ``none``, a
    usable inherited socket is used. Missing/unusable sockets raise
    ``SshAgentNoIdentityError``. Callers must already have raised
    ``ValidationError`` for malformed IdentityAgent values (no fallback).
    """

    if identity_agent is not None:
        return identity_agent

    inherited = (inherited_ssh_auth_sock or "").strip()
    if inherited and is_usable_agent_socket(Path(inherited)):
        return inherited

    raise SshAgentNoIdentityError(_NO_IDENTITY_MESSAGE)


__all__ = [
    "choose_effective_ssh_auth_sock",
    "destination_has_unsafe_characters",
    "is_usable_agent_socket",
    "parse_identity_agent_from_ssh_g",
    "ssh_destination_from_remote_url",
    "ssh_destination_from_scp_url",
    "ssh_destination_from_ssh_url",
    "token_looks_like_option",
    "validate_ssh_destination",
]
