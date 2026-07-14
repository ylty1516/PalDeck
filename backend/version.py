"""Application version — bump this when publishing a GitHub Release."""

APP_VERSION = "2.3.1"
# Override via env for testing
import os

APP_VERSION = os.environ.get("PALMOD_VERSION", APP_VERSION).lstrip("v")

GITHUB_OWNER = os.environ.get("PALMOD_GITHUB_OWNER", "ylty1516")
GITHUB_REPO = os.environ.get("PALMOD_GITHUB_REPO", "PalDeck")
