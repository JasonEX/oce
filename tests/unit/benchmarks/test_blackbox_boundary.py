import ast
from pathlib import Path


def test_blackbox_benchmarks_do_not_import_server_or_storage_implementations() -> None:
    root = Path(__file__).resolve().parents[3] / "benchmarks" / "blackbox"
    forbidden = (
        "benchmarks.internal",
        "oce",
        "oce_client",
        "pymilvus",
        "sqlalchemy",
        "sqlite3",
    )
    violations: list[str] = []

    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                modules = [node.module]
            else:
                continue
            for module in modules:
                if any(
                    module == name or module.startswith(f"{name}.")
                    for name in forbidden
                ):
                    violations.append(f"{path.name}:{node.lineno}: {module}")

    assert violations == []
