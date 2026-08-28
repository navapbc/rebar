"""Hermeticity guard: the unit tier must resolve AWS profile/config from an INJECTED
isolation dir, never the operator's real ``~/.aws``.

Regression guard for ``unhelpful-quartzitic-cardinal`` (864e-0fe2-820a-41ac). The
provider-parity tests drive the REAL production Bedrock path
(``build_bedrock_provider`` -> ``boto3.session.Session(region_name=...)``), whose
construction eagerly resolves the ambient ``AWS_PROFILE`` through botocore and reads it
from the operator's real ``~/.aws/config`` (the default ``AWS_CONFIG_FILE``). The
``_capture`` helper injected dummy ``AWS_ACCESS_KEY_ID``/``AWS_SECRET_ACCESS_KEY`` and a
region but never isolated the profile/config-FILE read, so under an empty/arbitrary
``$HOME`` the named profile is absent and Session construction raises
``botocore.exceptions.ProfileNotFound`` BEFORE ``converse`` -- the outbound payload is
never captured and six parity tests fail.

The tier fixture ``_isolated_aws_provider_credentials`` (``tests/unit/conftest.py``)
absorbs exactly that read: it redirects ``AWS_CONFIG_FILE`` and
``AWS_SHARED_CREDENTIALS_FILE`` into one injected per-session directory and neutralizes
``AWS_PROFILE``/``AWS_DEFAULT_PROFILE`` -- isolating ONLY the AWS credential/config read,
leaving ``$HOME`` and ``~/.gitconfig`` git identity untouched (the DIFFERENT seam this
ticket needs versus the MCP-scanner default-home seam of frousy-ornamental-whale).

This oracle is keyed on the INJECTED fixture dir (the paths the tier fixture exports),
never on POSIX-only paths and never on an assumption about where pytest's basetemp sits
relative to the real home -- so it fails deterministically on any machine when the tier
does not isolate the AWS read, and passes only when it does.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.unit


def test_unit_tier_isolates_aws_credential_resolution_from_real_home() -> None:
    """boto3/botocore must resolve AWS profile/config from the injected isolation dir.

    Fails RED before the tier fixture exists: ``AWS_CONFIG_FILE`` is unset, so botocore
    resolves ``~/.aws/config`` and honors the operator's ambient ``AWS_PROFILE`` -- the
    exact non-hermetic read that raised ``ProfileNotFound`` under an empty ``$HOME``.
    """
    import boto3

    config_file = os.environ.get("AWS_CONFIG_FILE")
    credentials_file = os.environ.get("AWS_SHARED_CREDENTIALS_FILE")

    # The tier must redirect BOTH botocore file locations into an injected isolation dir;
    # an unset value means botocore falls back to the operator's real ~/.aws.
    assert config_file, (
        "AWS_CONFIG_FILE is not redirected: the unit tier is reading the operator's "
        "real ~/.aws/config (non-hermetic)."
    )
    assert credentials_file, (
        "AWS_SHARED_CREDENTIALS_FILE is not redirected: the unit tier is reading the "
        "operator's real ~/.aws/credentials (non-hermetic)."
    )

    # Keyed on the injected dir itself: both files live in one injected isolation dir
    # that really exists (created for the tier), not the operator's ~/.aws.
    injected_dir = os.path.dirname(config_file)
    assert os.path.dirname(credentials_file) == injected_dir
    assert os.path.isdir(injected_dir)

    # The ambient operator profile must be neutralized; otherwise Session construction
    # resolves it against the config file and can raise ProfileNotFound.
    assert os.environ.get("AWS_PROFILE") is None
    assert os.environ.get("AWS_DEFAULT_PROFILE") is None

    # Behavioral: botocore's resolved config/credentials paths ARE the injected files,
    # and no profile is selected -- the exact resolution the production Bedrock Session
    # construction performs.
    session = boto3.session.Session()
    assert session._session.get_config_variable("config_file") == config_file
    assert session._session.get_config_variable("credentials_file") == credentials_file
    # No explicitly-named operator profile may be selected: botocore yields the implicit
    # ``default`` (or ``None``) when no profile env is set, never the operator's ambient
    # named profile (e.g. ``frontier``) that would be resolved against the real config.
    assert session.profile_name in (None, "default")
