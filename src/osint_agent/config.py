"""Configuration loading from settings.yaml and .env."""

from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel


class Config(BaseModel):
    """Top-level configuration."""

    rate_limits: dict[str, int] = {}
    tools: dict[str, bool] = {}
    storage: dict[str, str] = {}
    social: dict = {}


def load_config(
    config_path: Path = Path("config/settings.yaml"),
    env_path: Path = Path(".env"),
) -> Config:
    """Load settings from YAML and environment variables."""
    load_dotenv(env_path)

    if config_path.exists():
        with open(config_path) as f:
            raw = yaml.safe_load(f)
        return Config(**raw)

    return Config()
