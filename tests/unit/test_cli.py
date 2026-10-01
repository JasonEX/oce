"""CLI argument parsing and basic behaviour."""

from __future__ import annotations

import io
import os
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from oce import __version__
from oce.cli import _version, build_parser


def test_version_subcommand() -> None:
    args = build_parser().parse_args(["version"])
    assert args.command == "version"
    output = io.StringIO()
    with redirect_stdout(output):
        _version(args)
    assert output.getvalue().strip() == f"oce {__version__}"


def test_version_flag_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["--version"])
    assert exc_info.value.code == 0
    assert f"oce {__version__}" in capsys.readouterr().out


def test_verbose_flag_counts() -> None:
    assert build_parser().parse_args(["-v", "version"]).verbose == 1
    assert build_parser().parse_args(["-vv", "version"]).verbose == 2
    assert build_parser().parse_args(["version"]).verbose == 0


def test_init_creates_env_template(tmp_path: pytest.TempPathFactory) -> None:
    from oce import cli

    data_dir = tmp_path / "data"
    args = build_parser().parse_args(["init", "--data-dir", str(data_dir)])
    cli._init(args)

    content = (data_dir / ".env").read_text(encoding="utf-8")
    assert "API_KEY=" in content
    assert "EMBED_API_KEY=" in content
    assert "EMBED_MAX_QUERY_CHARS=3000" in content
    assert "RERANK_PROVIDER=api" in content
    assert "RERANK_LOCAL_MODEL_DIR=" in content
    assert "LLM_RERANK_ENABLED=false" in content


def test_init_refuses_overwrite_without_force(tmp_path: pytest.TempPathFactory) -> None:
    from oce import cli

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / ".env").write_text("keep-me\n", encoding="utf-8")

    args = build_parser().parse_args(["init", "--data-dir", str(data_dir)])
    with pytest.raises(SystemExit):
        cli._init(args)
    assert (data_dir / ".env").read_text(encoding="utf-8") == "keep-me\n"

    forced = build_parser().parse_args(["init", "--data-dir", str(data_dir), "--force"])
    cli._init(forced)
    assert "API_KEY=" in (data_dir / ".env").read_text(encoding="utf-8")


def test_personal_env_merges_local_overrides_before_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oce.cli import _load_personal_env
    from oce.shared.config.settings import LLMSettings, RetrievalSettings

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / ".env").write_text(
        "RETRIEVAL_FINAL_SELECT_K=11\nLLM_MODEL=base\n", encoding="utf-8"
    )
    (data_dir / ".env.local").write_text(
        "RETRIEVAL_FINAL_SELECT_K=7\nLLM_MODEL=local\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("RETRIEVAL_FINAL_SELECT_K", raising=False)
    monkeypatch.setenv("LLM_MODEL", "process")

    _load_personal_env(data_dir, None)

    assert RetrievalSettings(_env_file=None).final_select_k == 7
    assert LLMSettings(_env_file=None).model == "process"


def test_explicit_env_file_wins_without_loading_personal_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oce.cli import _load_personal_env
    from oce.shared.config.settings import RetrievalSettings

    (tmp_path / ".env.local").write_text(
        "RETRIEVAL_FINAL_SELECT_K=7\n", encoding="utf-8"
    )
    explicit = tmp_path / "selected.env"
    explicit.write_text("RETRIEVAL_FINAL_SELECT_K=11\n", encoding="utf-8")
    monkeypatch.setenv("RETRIEVAL_FINAL_SELECT_K", "13")

    _load_personal_env(tmp_path, str(explicit))

    assert RetrievalSettings(_env_file=None).final_select_k == 11


@pytest.mark.parametrize(
    (
        "explicit",
        "base_text",
        "local_text",
        "process_base",
        "expected_base",
        "expected_derived",
    ),
    [
        (
            False,
            "BASE=file\nDERIVED=${BASE}/endpoint",
            "",
            "process",
            "process",
            "process/endpoint",
        ),
        (
            True,
            "BASE=file\nDERIVED=${BASE}/endpoint",
            "",
            "process",
            "file",
            "file/endpoint",
        ),
        (
            False,
            "DERIVED=old\nBASE=base",
            "DERIVED=${BASE}/local",
            None,
            "base",
            "base/local",
        ),
        (
            False,
            "BASE=first\nDERIVED=${BASE}/endpoint\nBASE=last",
            "",
            None,
            "last",
            "first/endpoint",
        ),
        (
            False,
            "BASE=base",
            "BASE=local\nDERIVED=${BASE}/local",
            None,
            "local",
            "local/local",
        ),
    ],
)
def test_personal_env_preserves_variable_interpolation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    explicit: bool,
    base_text: str,
    local_text: str,
    process_base: str | None,
    expected_base: str,
    expected_derived: str,
) -> None:
    from oce.cli import _load_personal_env

    for name in ("CLI_BASE", "CLI_DERIVED"):
        monkeypatch.delenv(name, raising=False)
    if process_base is not None:
        monkeypatch.setenv("CLI_BASE", process_base)
    for filename, content in ((".env", base_text), (".env.local", local_text)):
        (tmp_path / filename).write_text(
            content.replace("BASE", "CLI_BASE").replace("DERIVED", "CLI_DERIVED"),
            encoding="utf-8",
        )

    _load_personal_env(tmp_path, str(tmp_path / ".env") if explicit else None)

    assert os.environ["CLI_BASE"] == expected_base
    assert os.environ["CLI_DERIVED"] == expected_derived
