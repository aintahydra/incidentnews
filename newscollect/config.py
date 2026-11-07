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
    refresh_seconds: int = 60  # how often the list page checks for new incidents; 0 = no auto-refresh
    llm_enabled: bool = True
    llm_provider: str = "local"  # "local" (Ollama) | "claude" (Anthropic API, ANTHROPIC_API_KEY)
    claude_model: str = "claude-sonnet-5-5"
    claude_effort: str = "low"
    llm_endpoint: str = "127.0.0.1:11434"
    llm_model: str = "solar:10.7b"
    llm_foreign_model: str = ""  # model for non-Korean articles; empty = same as llm_model
    llm_timeout: float = 180
    llm_num_ctx: int = 8192
    llm_think: bool = False  # Ollama "think": let thinking models reason before answering
    interval_minutes: int = 30
    max_articles_per_run: int = 30
    max_age_days: int = 30
    database: str = "data/news.db"
    wordcloud_trivial: list[str] = field(default_factory=list)
    gov_list: str = ""     # CSV of government bodies (name in column 2) -> "정부기관" tag
    public_list: str = ""  # CSV of public institutions (name in column 1) -> "공공기관" tag
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
    trivial = raw.get("wordcloud", {}).get("trivial", [])
    if not isinstance(trivial, list) or not all(isinstance(t, str) for t in trivial):
        raise ValueError("[wordcloud].trivial must be a list of strings")
    refresh = server.get("refresh_seconds", 60)
    if isinstance(refresh, bool) or not isinstance(refresh, (int, float)) or refresh < 0:
        raise ValueError(f"[server].refresh_seconds must be a number >= 0 (0 = off), not {refresh!r}")
    provider = llm.get("provider", "local")
    if provider not in ("local", "claude"):
        raise ValueError(f'[llm].provider must be "local" or "claude", not {provider!r}')
    effort = llm.get("claude_effort", "low")
    if effort not in ("low", "medium", "high", "xhigh", "max"):
        raise ValueError(f"[llm].claude_effort must be low|medium|high|xhigh|max, not {effort!r}")
    cfg = Config(
        host=server.get("host", "0.0.0.0"),
        port=int(server.get("port", 8000)),
        site_title=server.get("site_title", "보안 사고 타임라인"),
        refresh_seconds=0 if refresh == 0 else max(5, int(refresh)),  # floor 5s: don't hammer the server
        llm_enabled=bool(llm.get("enabled", True)),
        llm_provider=provider,
        claude_model=llm.get("claude_model", "claude-sonnet-5-5"),
        claude_effort=effort,
        llm_endpoint=llm.get("endpoint", "127.0.0.1:11434"),
        llm_model=llm.get("model", "solar:10.7b"),
        llm_foreign_model=llm.get("foreign_model", ""),
        llm_timeout=float(llm.get("timeout_seconds", 180)),
        llm_num_ctx=int(llm.get("num_ctx", 8192)),
        llm_think=bool(llm.get("think", False)),
        interval_minutes=int(col.get("interval_minutes", 30)),
        max_articles_per_run=int(col.get("max_articles_per_run", 30)),
        max_age_days=int(col.get("max_age_days", 30)),
        database=col.get("database", "data/news.db"),
        wordcloud_trivial=trivial,
        gov_list=raw.get("orgs", {}).get("government_list", ""),
        public_list=raw.get("orgs", {}).get("public_list", ""),
    )
    for s in raw.get("sources", []):
        if "url" not in s:
            raise ValueError(f"source missing url: {s}")
        cfg.sources.append(Source(s.get("name") or s["url"], s["url"], bool(s.get("enabled", True))))
    return cfg
