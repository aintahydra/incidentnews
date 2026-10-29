"""Load config.toml. Called on every collection cycle so edits apply live."""
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PATH = Path(os.environ.get("NEWSCOLLECT_CONFIG", "config.toml"))


@dataclass
class Source:
    name: str
    url: str
    enabled: bool = True


@dataclass
class Config:
    host: str = "0.0.0.0"
    port: int = 8000
    site_title: str = "보안 사고 타임라인"
    llm_enabled: bool = True
    llm_endpoint: str = "127.0.0.1:11434"
    llm_model: str = "solar:10.7b"
    llm_timeout: float = 180
    llm_num_ctx: int = 8192
    interval_minutes: int = 30
    max_articles_per_run: int = 30
    max_age_days: int = 30
    database: str = "data/news.db"
    sources: list[Source] = field(default_factory=list)

    @property
    def llm_base_url(self) -> str:
        ep = os.environ.get("OLLAMA_HOST") or self.llm_endpoint
        if not ep.startswith("http"):
            ep = "http://" + ep
        if not re.search(r":\d+$", ep):  # Ollama's own default port
            ep += ":11434"
        return ep


def load_config(path: Path | str | None = None) -> Config:
    path = Path(path or DEFAULT_PATH)
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    server, llm, col = raw.get("server", {}), raw.get("llm", {}), raw.get("collector", {})
    cfg = Config(
        host=server.get("host", "0.0.0.0"),
        port=int(server.get("port", 8000)),
        site_title=server.get("site_title", "보안 사고 타임라인"),
        llm_enabled=bool(llm.get("enabled", True)),
        llm_endpoint=llm.get("endpoint", "127.0.0.1:11434"),
        llm_model=llm.get("model", "solar:10.7b"),
        llm_timeout=float(llm.get("timeout_seconds", 180)),
        llm_num_ctx=int(llm.get("num_ctx", 8192)),
        interval_minutes=int(col.get("interval_minutes", 30)),
        max_articles_per_run=int(col.get("max_articles_per_run", 30)),
        max_age_days=int(col.get("max_age_days", 30)),
        database=col.get("database", "data/news.db"),
    )
    for s in raw.get("sources", []):
        if "url" not in s:
            raise ValueError(f"source missing url: {s}")
        cfg.sources.append(Source(s.get("name") or s["url"], s["url"], bool(s.get("enabled", True))))
    return cfg
