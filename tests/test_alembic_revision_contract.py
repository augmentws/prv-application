import ast
from pathlib import Path

VERSIONS_DIR = Path(__file__).parents[1] / "alembic" / "versions"


def _assignment(module: ast.Module, name: str):
    for node in module.body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
            and node.value is not None
        ):
            return ast.literal_eval(node.value)
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"Migration does not declare {name}")


def test_alembic_revision_ids_fit_the_version_table_and_form_a_valid_graph() -> None:
    revisions: dict[str, tuple[Path, str | tuple[str, ...] | None]] = {}
    for path in sorted(VERSIONS_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        module = ast.parse(path.read_text(), filename=str(path))
        revision = _assignment(module, "revision")
        down_revision = _assignment(module, "down_revision")
        assert isinstance(revision, str), f"{path.name} revision must be a string"
        assert len(revision) <= 32, (
            f"{path.name} revision {revision!r} is {len(revision)} characters; "
            "alembic_version.version_num is VARCHAR(32)"
        )
        assert revision not in revisions, f"Duplicate Alembic revision {revision!r}"
        revisions[revision] = (path, down_revision)

    for revision, (path, down_revision) in revisions.items():
        parents = down_revision if isinstance(down_revision, tuple) else (down_revision,)
        for parent in parents:
            if parent is not None:
                assert parent in revisions, (
                    f"{path.name} revision {revision!r} references missing parent {parent!r}"
                )
