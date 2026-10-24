from pathlib import Path
from tempfile import TemporaryDirectory as _TemporaryDirectory


def temporary_directory():
    """Keep test files inside this workspace, including under restricted runners."""
    root = Path(__file__).resolve().parent.parent
    parent = (root / "data" / "test-tmp").resolve()
    if not parent.is_relative_to(root):
        raise RuntimeError("Test directory escaped workspace")
    parent.mkdir(parents=True, exist_ok=True)
    return _TemporaryDirectory(prefix="test-", dir=parent)
