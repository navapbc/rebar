"""Unit tests isolate AWS profile and config reads from the operator's home. The Bedrock provider
path uses injected credential files. This guard catches ambient AWS_PROFILE or ~/.aws leakage
without calling the network."""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.unit


def test_unit_tier_isolates_aws_credential_resolution_from_real_home() -> None:
    """Botocore must read profile and config from the injected isolation directory, never the
    operator's ~/.aws."""
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
