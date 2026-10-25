"""Parity: CLI and SPA cover the REST contract exactly."""

from __future__ import annotations

import json
from pathlib import Path

import typer.main

from opal_talk.cli.client import OPERATIONS
from opal_talk.cli.main import COMMANDS, app
from opal_talk.contract import contract_operations

SPA = Path(__file__).resolve().parents[1] / "spa"


def test_cli_client_operation_map_matches_contract():
    assert contract_operations() == OPERATIONS


def test_every_operation_has_a_cli_command_that_exists():
    assert set(COMMANDS) == set(contract_operations())
    root = typer.main.get_command(app)
    talk = root.commands["talk"]  # type: ignore[attr-defined]
    for op_id, command in COMMANDS.items():
        node = talk
        for word in command.split():
            if word.startswith("--"):
                assert word in {opt for p in node.params for opt in p.opts}, f"{op_id}: option {word} missing"
                continue
            node = node.commands.get(word)  # type: ignore[attr-defined]
            assert node is not None, f"{op_id}: CLI command '{command}' does not exist"


def test_spa_operation_map_matches_contract():
    ops = json.loads((SPA / "src/api/operations.json").read_text(encoding="utf-8"))
    assert {k: (v["method"], v["path"]) for k, v in ops.items()} == contract_operations()


def test_spa_ui_uses_every_operation():
    code = "\n".join(
        p.read_text(encoding="utf-8")
        for p in (SPA / "src").rglob("*.ts*")
        if not {"api", "test"} & set(p.relative_to(SPA / "src").parts[:-1])
    )
    unused = [op for op in contract_operations() if f"'{op}'" not in code]
    assert unused == []
