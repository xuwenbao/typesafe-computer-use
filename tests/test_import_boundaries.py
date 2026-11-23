"""Code that runs where no desktop adapter can load (the Linux sandbox, a server, the WAA client).

Read from the source rather than by importing: every module the browser package reaches must
stay clear of the platform adapter, or `clicker-bench` needs macOS-only packages to start. The
same holds for the readers of a WAA VM's tree and of its results, which run in the client container.
"""

import ast
import sys
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parent.parent / "typesafe_computer_use"
PLATFORM = {"platform_adapter", "macos", "windows", "perception"}
# The desktop perception's models: the browser path reads the DOM and must not pull in PyTorch.
MODELS = {"parse.ocr", "parse.detect", "parse.caption", "parse.fuse", "parse.marks", "parse.weights", "parse.runtime"}


def module_level(tree: ast.AST):
    """Every node that runs on import. An import inside a function runs only when it is called,
    as the OCR comparison in `clicker-bench perception` does."""
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        yield node
        yield from module_level(node)


def imports_of(path: Path) -> set[str]:
    """The package modules a file imports when it loads, as dotted names relative to the package."""
    here = path.relative_to(PACKAGE).with_suffix("").parts[:-1]
    found = set()
    for node in module_level(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.level:
            base = list(here[: len(here) - (node.level - 1)]) if node.level > 1 else list(here)
            if node.module:
                base += node.module.split(".")
                found.add(".".join(base))
            else:
                found.update(".".join([*base, alias.name]) for alias in node.names)
    return found


def module_file(name: str) -> Path | None:
    for candidate in (PACKAGE / Path(*name.split("."))).with_suffix(".py"), PACKAGE / Path(*name.split(".")) / "__init__.py":
        if candidate.exists():
            return candidate
    return None


def test_uia_roles_imports_nothing_but_the_standard_library():
    """The VM walk concatenates this file into a script that cannot import jev or anything installed."""
    tree = ast.parse((PACKAGE / "uia_roles.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "uia_roles imports another module of jev"
            if node.module:
                imported.add(node.module.split(".")[0])
    assert imported <= sys.stdlib_module_names


def test_only_desk_lock_imports_win32event():
    """The mutex needs pywin32. The gate and the service stay importable without it."""
    users = set()
    for path in (PACKAGE / "helper").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in module_level(tree):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            if any(name.split(".")[0] == "win32event" for name in names):
                users.add(path.stem)
    assert users == {"desk_lock"}


def function_imports(path: Path, fn_name: str) -> set[str]:
    """The package modules one function imports, as dotted names relative to the package."""
    here = path.relative_to(PACKAGE).with_suffix("").parts[:-1]
    found = set()
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) or node.name != fn_name:
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.ImportFrom) and child.level:
                base = list(here[: len(here) - (child.level - 1)]) if child.level > 1 else list(here)
                if child.module:
                    base += child.module.split(".")
                    found.add(".".join(base))
                else:
                    found.update(".".join([*base, alias.name]) for alias in child.names)
    return found


def test_the_mcp_command_imports_only_the_front():
    assert function_imports(PACKAGE / "helper" / "cli.py", "serve_mcp") == {"helper.front"}


def test_the_setup_command_imports_only_the_guide():
    assert function_imports(PACKAGE / "helper" / "cli.py", "run_setup") == {"helper.setup"}


def test_uia_patterns_imports_nothing_but_the_standard_library():
    """The same script concatenates this file, so it stays free of jev and of Windows libraries."""
    tree = ast.parse((PACKAGE / "uia_patterns.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "uia_patterns imports another module of jev"
            if node.module:
                imported.add(node.module.split(".")[0])
    assert imported <= sys.stdlib_module_names
    assert not imported & {"win32api", "uiautomation", "comtypes", "pywin32"}


def test_the_session_controller_is_not_on_the_macos_import_path():
    """clicker on macOS never loads the child-session module, and that module never loads the adapter."""
    assert "windows_session" not in imports_of(PACKAGE / "cli.py")
    assert "windows_session" not in imports_of(PACKAGE / "windows.py")
    assert "platform_adapter" not in imports_of(PACKAGE / "windows_session.py")


@pytest.mark.parametrize("start", ["browser", "waa.tree", "waa.results", "helper.front", "helper.setup"])
def test_never_reaches_the_desktop_adapter(start):
    seen, todo = set(), [start]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        path = module_file(name)
        if path is None:
            continue
        if path.name == "__init__.py":
            todo.extend(f"{name}.{p.stem}" for p in path.parent.glob("*.py") if p.stem != "__init__")
        todo.extend(imports_of(path))
    assert not {name.split(".")[-1] for name in seen} & PLATFORM, sorted(seen)
    assert not seen & MODELS, sorted(seen)
