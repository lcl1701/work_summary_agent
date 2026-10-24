"""Local release metadata and file fingerprints; never reads credentials or data."""

import hashlib
from pathlib import Path


VERSION = "0.3.0"
APP_ROOT = Path(__file__).resolve().parent
CHANGES = [{
    "version": VERSION, "date": "2026-10-23",
    "summary": "按用途配置汇报模板与沟通框架，预览确认保存，保留生成配置及原始消息依据。",
    "dependencies": "沿用 Python 标准库、SQLite 和原生网页，无新增系统软件或第三方运行依赖。",
}, {
    "version": "0.2.0", "date": "2026-10-22",
    "summary": "姓名显示方式、页面更新提示、持久操作记录与按条审阅报告。",
    "dependencies": "沿用 Python 标准库、SQLite 和原生网页，无新增系统软件或第三方运行依赖。",
}]


def _fingerprint(paths, root):
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def backend_build(root=APP_ROOT):
    return _fingerprint(root.glob("*.py"), root)


def page_build(root=APP_ROOT):
    paths = list(root.glob("*.py")) + [p for p in (root / "static").glob("*") if p.is_file()]
    return _fingerprint(paths, root)
