"""Service (process) configuration loaded from YAML with environment overrides."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_FILE = Path("config/service.yaml")


@dataclass
class ServiceConfig:
    host: str = "127.0.0.1"
    port: int = 8001
    data_dir: Path = Path("var")
    config_dir: Path = Path("config")
    log_level: str = "info"
    cors_origins: list[str] = field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"]
    )
    spa_dir: Path | None = None
    # engine_id -> {"enabled": bool, "options": {...}}; used to seed engine state on first start.
    engines: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "opal_talk.db"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"


def load_config(path: Path | str | None = None) -> ServiceConfig:
    """Load YAML config from `path`, $OPAL_TALK_CONFIG or ./config/service.yaml (if present)."""
    if path is None:
        env = os.environ.get("OPAL_TALK_CONFIG")
        path = Path(env) if env else (DEFAULT_CONFIG_FILE if DEFAULT_CONFIG_FILE.is_file() else None)
    raw: dict[str, Any] = {}
    if path is not None:
        with open(path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"service config '{path}' must be a YAML mapping")
    raw = raw.get("service", raw)

    cfg = ServiceConfig()
    for key in ("host", "log_level"):
        if key in raw:
            setattr(cfg, key, str(raw[key]))
    if "port" in raw:
        cfg.port = int(raw["port"])
    for key in ("data_dir", "config_dir", "spa_dir"):
        if raw.get(key):
            setattr(cfg, key, Path(str(raw[key])).expanduser())
    if "cors_origins" in raw:
        cfg.cors_origins = [str(o) for o in raw["cors_origins"] or []]
    if isinstance(raw.get("engines"), dict):
        cfg.engines = {str(k): dict(v or {}) for k, v in raw["engines"].items()}

    env_map = {
        "OPAL_TALK_HOST": ("host", str),
        "OPAL_TALK_PORT": ("port", int),
        "OPAL_TALK_DATA_DIR": ("data_dir", lambda v: Path(v).expanduser()),
        "OPAL_TALK_CONFIG_DIR": ("config_dir", lambda v: Path(v).expanduser()),
        "OPAL_TALK_SPA_DIR": ("spa_dir", lambda v: Path(v).expanduser()),
    }
    for env_key, (attr, conv) in env_map.items():
        if os.environ.get(env_key):
            setattr(cfg, attr, conv(os.environ[env_key]))
    return cfg
