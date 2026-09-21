"""Guards against model/migration drift.

There is no Postgres in CI here, so instead of running the migration we parse it and
compare the columns it creates against the SQLAlchemy metadata. Catches the common
failure where a model gains a column and the migration does not.
"""

import ast
import pathlib

from app.models import Base

VERSIONS = pathlib.Path(__file__).resolve().parents[1] / "migrations" / "versions"


def _is_column(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "Column"
        and bool(node.args)
    )


def _upgrade_calls(path: pathlib.Path) -> list[ast.Call]:
    """op.* calls inside upgrade(), in source order."""
    tree = ast.parse(path.read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "upgrade")
    calls = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and isinstance(n.func.value, ast.Name) and n.func.value.id == "op"
    ]
    return sorted(calls, key=lambda n: (n.lineno, n.col_offset))


def _columns_created_by_migration() -> dict[str, set[str]]:
    """Replay every migration's create_table / add_column / drop_column, in order."""
    tables: dict[str, set[str]] = {}
    for path in sorted(VERSIONS.glob("[0-9]*.py")):
        for node in _upgrade_calls(path):
            op_name = node.func.attr
            if op_name == "create_table":
                tables[node.args[0].value] = {
                    a.args[0].value for a in node.args[1:] if _is_column(a)
                }
            elif op_name == "add_column":
                tables[node.args[0].value].add(node.args[1].args[0].value)
            elif op_name == "drop_column":
                tables[node.args[0].value].discard(node.args[1].value)
            elif op_name == "drop_table":
                tables.pop(node.args[0].value, None)
    return tables


def test_every_model_table_is_created():
    migrated = _columns_created_by_migration()
    assert set(Base.metadata.tables) == set(migrated)


def test_columns_match_per_table():
    migrated = _columns_created_by_migration()
    for name, table in Base.metadata.tables.items():
        model_cols = {c.name for c in table.columns}
        assert model_cols == migrated[name], (
            f"{name}: only in model {sorted(model_cols - migrated[name])}, "
            f"only in migration {sorted(migrated[name] - model_cols)}"
        )


def test_job_status_enum_includes_paused():
    """Spec section 15: a Jina 402 pauses the job rather than failing domains."""
    from app.models import JOB_STATUSES

    assert "paused" in JOB_STATUSES
