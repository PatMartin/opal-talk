"""Backup/restore: `.tar.gz` archives containing a single logical `ConfigExport` JSON
document; they never contain engine binaries or voice/model files. Restores run the
shared candidate pipeline (validate-all-then-apply) from `config_candidate`."""

from __future__ import annotations

import io
import json
import re
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opal_talk.schemas import ConfigExport
from opal_talk.service.config_candidate import Candidate, CandidateService
from opal_talk.service.errors import Conflict, InvalidRequest, NotFound, OperationFailed
from opal_talk.service.settings import settings_to_str

BACKUP_MEMBER = "opal-talk-config.json"
BACKUP_PATTERN = re.compile(r"^opal-talk-\d{4}-\d{2}-\d{2}T\d{6}Z(-\d+)?\.tar\.gz$")


class BackupService:
    def __init__(self, candidates: CandidateService, backup_dir: Path):
        self.candidates = candidates
        self.backup_dir = backup_dir

    def _path(self, filename: str) -> Path:
        if filename in (".", "..") or ".." in filename or "/" in filename or "\\" in filename or "\x00" in filename:
            raise InvalidRequest.single(["path", "filename"], "invalid backup filename", "value_error", filename)
        path = self.backup_dir / filename
        if not BACKUP_PATTERN.match(filename) or not path.is_file():
            raise NotFound(f"backup '{filename}' not found", "backup_not_found", {"filename": filename})
        return path

    def list_backups(self) -> list[dict[str, Any]]:
        if not self.backup_dir.is_dir():
            return []
        items = []
        for path in self.backup_dir.iterdir():
            if path.is_file() and BACKUP_PATTERN.match(path.name):
                stat = path.stat()
                items.append(
                    {
                        "filename": path.name,
                        "created_at": datetime.fromtimestamp(stat.st_mtime, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                        "size_bytes": stat.st_size,
                    }
                )
        return sorted(items, key=lambda b: (b["created_at"], b["filename"]), reverse=True)

    def create_backup(self) -> dict[str, Any]:
        try:
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            stem = datetime.now(UTC).strftime("opal-talk-%Y-%m-%dT%H%M%SZ")
            filename, n = f"{stem}.tar.gz", 1
            while (self.backup_dir / filename).exists():
                filename, n = f"{stem}-{n}.tar.gz", n + 1
            payload = json.dumps(ConfigExport.model_validate(self.candidates.export()).model_dump(), indent=2).encode()
            tmp = self.backup_dir / f".{filename}.tmp"
            with tarfile.open(tmp, "w:gz") as tar:
                info = tarfile.TarInfo(BACKUP_MEMBER)
                info.size = len(payload)
                info.mtime = int(datetime.now(UTC).timestamp())
                info.mode = 0o600
                tar.addfile(info, io.BytesIO(payload))
            tmp.replace(self.backup_dir / filename)
        except OSError as exc:
            raise OperationFailed(f"failed to create backup: {exc}", "backup_failed") from exc
        return {"filename": filename, "message": "Backup created successfully."}

    def backup_path(self, filename: str) -> Path:
        return self._path(filename)

    def delete_backup(self, filename: str) -> None:
        self._path(filename).unlink()

    def restore_backup(self, filename: str) -> dict[str, Any]:
        path = self._path(filename)
        try:
            with tarfile.open(path, "r:gz") as tar:
                member = tar.extractfile(tar.getmember(BACKUP_MEMBER))
                data = json.loads(member.read().decode("utf-8")) if member else None
            export = ConfigExport.model_validate(data)
        except (tarfile.TarError, KeyError, OSError, ValueError, UnicodeDecodeError) as exc:
            raise Conflict(f"backup '{filename}' is not a valid OPAL Talk backup: {exc}", "invalid_backup") from exc

        cand = Candidate(
            engines={e.engine_id: {"enabled": e.enabled, "options": e.options} for e in export.engines},
            voices=[v.model_dump() for v in export.voices],
            profiles=[p.model_dump() for p in export.profiles],
            settings=settings_to_str(export.settings),
        )
        report, descriptions = self.candidates.validate_candidate(cand, resources_are_errors=False)
        if not report.valid:
            raise Conflict(
                f"backup '{filename}' failed validation; nothing was restored",
                "backup_invalid",
                {"errors": [{"code": i.code, "message": i.message, "field": i.field} for i in report.errors]},
            )
        try:
            self.candidates.apply(cand, descriptions)
        except Exception as exc:  # noqa: BLE001
            raise OperationFailed(f"failed to restore backup: {exc}", "restore_failed") from exc
        return {
            "success": True,
            "message": f"Backup restored successfully: {self.candidates.summary(cand)}.",
            "details": {
                "filename": filename,
                "warnings": [{"code": i.code, "message": i.message, "field": i.field} for i in report.warnings],
                **report.details,
            },
        }
