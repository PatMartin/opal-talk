"""Configuration export/import/validation and backup/restore — facade.

The work lives in two cohesive units: `CandidateService` (candidate configuration
pipeline in `config_candidate`) and `BackupService` (`.tar.gz` archive I/O in
`backups`). This class keeps the historical `ConfigService` API surface stable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from opal_talk.persistence.store import Store
from opal_talk.service.backups import BackupService
from opal_talk.service.catalog import EngineService
from opal_talk.service.config_candidate import Candidate, CandidateService
from opal_talk.tts.base import Report

CONFIG_FILES = ("engines.yaml", "voices.yaml", "profiles.yaml", "settings.yaml")


class ConfigService:
    def __init__(self, store: Store, engines: EngineService, config_dir: Path, backup_dir: Path):
        self.candidates = CandidateService(store, engines, config_dir)
        self.backups = BackupService(self.candidates, backup_dir)

    # -- candidate pipeline ---------------------------------------------------

    def export(self) -> dict[str, Any]:
        return self.candidates.export()

    def validate_candidate(self, cand: Candidate, resources_are_errors: bool) -> tuple[Report, dict[str, dict]]:
        return self.candidates.validate_candidate(cand, resources_are_errors)

    def validate_current(self) -> Report:
        return self.candidates.validate_current()

    def import_config(self) -> dict[str, Any]:
        return self.candidates.import_config()

    # -- backups ----------------------------------------------------------------

    def list_backups(self) -> list[dict[str, Any]]:
        return self.backups.list_backups()

    def create_backup(self) -> dict[str, Any]:
        return self.backups.create_backup()

    def backup_path(self, filename: str) -> Path:
        return self.backups.backup_path(filename)

    def delete_backup(self, filename: str) -> None:
        self.backups.delete_backup(filename)

    def restore_backup(self, filename: str) -> dict[str, Any]:
        return self.backups.restore_backup(filename)
