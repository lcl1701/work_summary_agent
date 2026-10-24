from dataclasses import dataclass, field
import os
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent


def read_env(path: Path) -> dict[str, str]:
    """Small .env reader; never execute file contents or overwrite process env."""
    values = {}
    if path.exists():
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise ValueError(".env 中每个配置项应使用 KEY=value 格式")
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key.strip()] = value
    return values


@dataclass(frozen=True)
class Settings:
    port: int = 8765
    data_dir: Path = ROOT / "data"
    feishu_app_id: str = ""
    feishu_app_secret: str = field(default="", repr=False)
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = field(default="", repr=False)
    llm_model: str = ""

    @property
    def feishu_configured(self) -> bool:
        return bool(self.feishu_app_id and self.feishu_app_secret)

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_api_key and self.llm_model)


def load_settings(path: Path | None = None, environ=None) -> Settings:
    values = read_env(path or ROOT / ".env")
    values.update(os.environ if environ is None else environ)
    port = int(values.get("APP_PORT", "8765"))
    if not 1 <= port <= 65535:
        raise ValueError("APP_PORT 必须在 1–65535 之间")
    data_dir = Path(values.get("DATA_DIR", str(ROOT / "data")))
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    base = (values.get("LLM_BASE_URL", "").strip() or "https://api.openai.com/v1").rstrip("/")
    parsed = urlsplit(base)
    if (parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    )) or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("LLM_BASE_URL 应为 HTTPS API 地址（本机模型可使用 HTTP）")
    return Settings(port, data_dir.resolve(), values.get("FEISHU_APP_ID", "").strip(),
                    values.get("FEISHU_APP_SECRET", "").strip(), base,
                    values.get("LLM_API_KEY", "").strip(), values.get("LLM_MODEL", "").strip())
