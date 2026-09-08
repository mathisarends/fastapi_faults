import ast
from pathlib import Path

SOURCE_ROOT = Path(__file__).parents[2] / "src" / "fastapi_faults"


def source_modules() -> list[Path]:
    return sorted(SOURCE_ROOT.glob("*.py"))


def test_product_modules_use_modern_annotation_syntax() -> None:
    for path in source_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

        assert ast.get_docstring(tree, clean=False) is None, path
        assert not any(
            isinstance(node, ast.ImportFrom) and node.module == "__future__"
            for node in ast.walk(tree)
        ), path
        assert not any(
            isinstance(node, ast.ImportFrom)
            and node.module == "typing"
            and any(alias.name in {"Generic", "TypeVar"} for alias in node.names)
            for node in ast.walk(tree)
        ), path


def test_product_modules_do_not_hide_modules_or_loggers() -> None:
    assert {path.name for path in source_modules() if path.name.startswith("_")} == {
        "__init__.py"
    }

    for path in source_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        assert not any(
            isinstance(node, ast.Name) and node.id == "_logger"
            for node in ast.walk(tree)
        ), path
