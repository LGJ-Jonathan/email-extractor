"""Guards against model/migration drift.

There is no Postgres in CI here, so instead of running the migration we parse it and
compare the columns it creates against the SQLAlchemy metadata. Catches the common
failure where a model gains a column and the migration does not.
"""

import ast
import pathlib

from app.models import Base

MIGRATION = pathlib.Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0001_initial.py"


def _columns_created_by_migration() -> dict[str, set[str]]:
    tree = ast.parse(MIGRATION.read_text())
    tables: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "create_table" or not node.args:
            continue
        table_name = node.args[0].value
        cols: set[str] = set()
        for arg in node.args[1:]:
            if (
                isinstance(arg, ast.Call)
                and isinstance(arg.func, ast.Attribute)
                and arg.func.attr == "Column"
                and arg.args
            ):
                cols.add(arg.args[0].value)
        tables[table_name] = cols
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
