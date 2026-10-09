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
        assert hasattr(settings, "CELERY_BROKER_URL")
        assert hasattr(settings, "TEMP_REPO_BASE_PATH")
        assert hasattr(settings, "GIT_ACCESS_TOKEN")
        assert hasattr(settings, "CREDENTIAL_ENCRYPTION_KEY")

    @patch.dict(
        os.environ,
        {
            "CREDENTIAL_ENCRYPTION_KEY": "Q1PNlFd4It9oQPtjCcXcmB7wGDkY4w8KwpIRNSF4u7U=",
            "DATABASE_URL": "postgresql+psycopg2://appstore_worker:pw@postgres:5432/appstore",
            "GIT_ACCESS_TOKEN": "test-token",
        },
        clear=True,
    )
    def test_settings_from_environment(self):
        """Test loading settings from environment variables."""
        from importlib import reload

        from app import config

        reload(config)

        # The broker defaults to the database, through the pgq transport.
        assert config.settings.celery_broker_url == "pgq+postgresql://appstore_worker:pw@postgres:5432/appstore"
        assert config.settings.GIT_ACCESS_TOKEN == "test-token"

    @patch.dict(
        os.environ,
        {
            "CREDENTIAL_ENCRYPTION_KEY": "Q1PNlFd4It9oQPtjCcXcmB7wGDkY4w8KwpIRNSF4u7U=",
            "DATABASE_URL": "postgresql://u:p@db/x",
            "CELERY_BROKER_URL": "pgq+postgresql://other@db/y",
        },
        clear=True,
    )
    def test_explicit_broker_url_wins(self):
        from importlib import reload

        from app import config

        reload(config)
        assert config.settings.celery_broker_url == "pgq+postgresql://other@db/y"
