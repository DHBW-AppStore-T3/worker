"""Tests for configuration."""

import os
from unittest.mock import patch

import pytest


@pytest.mark.unit
class TestConfiguration:
    """Test configuration loading."""

    def test_settings_import(self):
        """Test that settings can be imported."""
        from app.config import settings

        assert settings is not None

    def test_required_settings_exist(self):
        """Test that required settings attributes exist."""
        from app.config import settings

        assert hasattr(settings, "DATABASE_URL")
        # The VM path (OpenTofu, Packer, OpenStack, Git clone) is gone (.github#12),
        # and so is Celery with its broker: the queue is the tasks table.
        for gone in (
            "TEMP_REPO_BASE_PATH",
            "TOFU_PATH",
            "PACKER_PATH",
            "OPENSTACK_ALLOWED_AUTH_URLS",
            "WORKER_JOB_UID_BASE",
            "CELERY_BROKER_URL",
            "WORKER_QUEUE_VISIBILITY_SECONDS",
        ):
            assert not hasattr(settings, gone)
        assert hasattr(settings, "CREDENTIAL_ENCRYPTION_KEY")

    @patch.dict(
        os.environ,
        {
            "APP_ENV": "development",
            "CREDENTIAL_ENCRYPTION_KEY": "Q1PNlFd4It9oQPtjCcXcmB7wGDkY4w8KwpIRNSF4u7U=",
            "DATABASE_URL": "postgresql+psycopg2://appstore_worker:pw@postgres:5432/appstore",
        },
        clear=True,
    )
    def test_settings_from_environment(self):
        """Test loading settings from environment variables."""
        from importlib import reload

        from app import config

        reload(config)

        assert config.settings.DATABASE_URL == "postgresql+psycopg2://appstore_worker:pw@postgres:5432/appstore"
        assert config.settings.WORKER_CONCURRENCY == 4
        assert config.settings.WORKER_POLL_INTERVAL_SECONDS == 5.0

    def test_the_database_url_loses_its_driver_suffix_for_libpq(self):
        from app.db import libpq_url

        assert libpq_url("postgresql+psycopg2://u:p@db:5432/x") == "postgresql://u:p@db:5432/x"
        assert libpq_url("postgresql://u:p@db/x") == "postgresql://u:p@db/x"

    @pytest.mark.parametrize(
        ("env", "message"),
        [
            ({"CREDENTIAL_ENCRYPTION_KEY": "Q1PNlFd4It9oQPtjCcXcmB7wGDkY4w8KwpIRNSF4u7U="}, "published test key"),
            ({"CREDENTIAL_ENCRYPTION_KEY": "not-a-key", "APP_ENV": "development"}, "url-safe base64"),
            ({"APP_ENV": "development"}, "CREDENTIAL_ENCRYPTION_KEY or RESULTS_ENCRYPTION_KEY must be set"),
            (
                {"RESULTS_ENCRYPTION_KEY": "Q1PNlFd4It9oQPtjCcXcmB7wGDkY4w8KwpIRNSF4u7U="},
                "RESULTS_ENCRYPTION_KEY is a published test key",
            ),
            ({"RESULTS_ENCRYPTION_KEY": "not-a-key", "APP_ENV": "development"}, "RESULTS_ENCRYPTION_KEY is not url"),
            (
                {
                    "CREDENTIAL_ENCRYPTION_KEY": "yJdwFtdm0sMMuyiokYEj4Fbt6lDnkhfRV9PqcSvhvxQ=",
                    "RESULTS_ENCRYPTION_KEY": "yJdwFtdm0sMMuyiokYEj4Fbt6lDnkhfRV9PqcSvhvxQ=",
                },
                "RESULTS_ENCRYPTION_KEY is the credential key",
            ),
            (
                {
                    "CREDENTIAL_ENCRYPTION_KEY": "yJdwFtdm0sMMuyiokYEj4Fbt6lDnkhfRV9PqcSvhvxQ=",
                    "WORKER_SIMULATE": "true",
                },
                "WORKER_SIMULATE requires APP_ENV=development",
            ),
        ],
    )
    def test_unsafe_keys_never_start(self, env, message):
        from pydantic import ValidationError

        from app.config import Settings

        with (
            patch.dict(os.environ, {"DATABASE_URL": "postgresql://u:p@db/x", **env}, clear=True),
            pytest.raises(ValidationError, match=message),
        ):
            Settings(_env_file=None)

    def test_the_pod_deployer_starts_with_the_results_key_alone(self):
        from cryptography.fernet import Fernet

        from app.config import Settings

        key = Fernet.generate_key().decode()
        with patch.dict(
            os.environ, {"DATABASE_URL": "postgresql://u:p@db/x", "RESULTS_ENCRYPTION_KEY": key}, clear=True
        ):
            settings = Settings(_env_file=None)
        assert key == settings.RESULTS_ENCRYPTION_KEY
        assert not settings.CREDENTIAL_ENCRYPTION_KEY

    def test_production_with_a_real_key_starts(self):
        from cryptography.fernet import Fernet

        from app.config import Settings

        key = Fernet.generate_key().decode()
        with patch.dict(
            os.environ, {"DATABASE_URL": "postgresql://u:p@db/x", "CREDENTIAL_ENCRYPTION_KEY": key}, clear=True
        ):
            settings = Settings(_env_file=None)
        assert settings.APP_ENV == "production"
        assert not hasattr(settings, "TFSTATE_DATABASE_URL")
