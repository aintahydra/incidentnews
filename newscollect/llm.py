"""LLM backends behind one call: chat(model, system, user) -> text.

- "local":  Ollama /api/chat (endpoint + model from [llm], unchanged behavior)
- "claude": Anthropic Messages API (key from ANTHROPIC_API_KEY, model from [llm].claude_model)

Errors are normalized so callers don't care which backend runs:
- LLMError: this answer is unusable (bad HTTP status, refusal, empty reply) -> callers retry or give up on the article
- LLMUnavailable: the service can't be reached or the credentials are wrong -> the collector stops for this cycle
  without burning the remaining articles' retry attempts
"""
import os

import httpx


class LLMError(Exception):
    pass


class LLMUnavailable(Exception):
    pass


class Ollama:
    def __init__(self, client: httpx.Client, base_url: str, num_ctx: int = 8192, think: bool = False):
        self.client, self.base_url, self.num_ctx, self.think = client, base_url, num_ctx, think

    def chat(self, model: str, system: str, user: str, *, json: bool = False, temperature: float = 0.0,
             max_tokens: int = 1000) -> str:
        body = {"model": model, "stream": False,
                "options": {"temperature": temperature, "num_ctx": self.num_ctx, "num_predict": max_tokens},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                # thinking models (gemma4, qwen3, ...) otherwise reason first, and that reasoning counts toward
                # num_predict: the JSON answer came back empty or cut off. Models without thinking ignore it.
                "think": self.think}
        if json:
            body["format"] = "json"
        try:
            r = self.client.post(f"{self.base_url}/api/chat", json=body)
            r.raise_for_status()
            return r.json()["message"]["content"]
        except httpx.TransportError as e:
            raise LLMUnavailable(f"Ollama 연결 실패: {e}") from e
        except httpx.HTTPStatusError as e:  # transient 5xx (model loading, OOM): the caller retries
            raise LLMError(f"Ollama HTTP {e.response.status_code}") from e
        except (KeyError, ValueError) as e:
            raise LLMError(f"Ollama 응답 형식 오류: {type(e).__name__}") from e


class Claude:
    """Claude via the Anthropic SDK. temperature is accepted for interface parity but not sent: current models reject
    non-default sampling values. Thinking stays adaptive (the model default); `effort` sets how much it thinks."""
    MAX_TOKENS = 16000  # thinking counts toward max_tokens; the visible answers here are short
    # cyber-category refusals (security news can trip them) are re-run server-side on the fallback model
    BETAS = ["server-side-fallback-2026-07-01"]

    def __init__(self, client, effort: str = "low"):
        self.client, self.effort = client, effort

    def chat(self, model: str, system: str, user: str, *, json: bool = False, temperature: float = 0.0,
             max_tokens: int = 1000) -> str:
        import anthropic
        try:
            resp = self.client.beta.messages.create(
                model=model, max_tokens=self.MAX_TOKENS, system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"effort": self.effort},
                betas=self.BETAS, fallbacks="default")
        except (anthropic.APIConnectionError, anthropic.AuthenticationError, anthropic.PermissionDeniedError,
                anthropic.RateLimitError) as e:  # the SDK already retried connection errors and 429s
            raise LLMUnavailable(f"Claude API 사용 불가: {type(e).__name__}: {e}") from e
        except anthropic.APIStatusError as e:  # 400 / 5xx after retries: this request failed
            raise LLMError(f"Claude API 오류 {e.status_code}: {e.message}") from e
        if resp.stop_reason == "refusal":
            cat = getattr(resp.stop_details, "category", None) if resp.stop_details else None
            raise LLMError(f"Claude가 요청을 거부함(분류: {cat})")
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        if not text:
            raise LLMError(f"Claude 응답에 텍스트가 없음(stop_reason={resp.stop_reason})")
        return text


def from_config(cfg, http: httpx.Client):
    """The backend selected by [llm].provider. `http` is the shared httpx client used for Ollama."""
    if cfg.llm_provider == "claude":
        import anthropic
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise LLMUnavailable("[llm].provider = \"claude\"이지만 ANTHROPIC_API_KEY가 설정되지 않음(.env 확인)")
        return Claude(anthropic.Anthropic(timeout=cfg.llm_timeout), cfg.claude_effort)
    return Ollama(http, cfg.llm_base_url, cfg.llm_num_ctx, getattr(cfg, "llm_think", False))


def model_for(cfg, korean: bool) -> str:
    """claude: one model for everything. local: `model` for Korean, `foreign_model` (if set) for other languages."""
    if getattr(cfg, "llm_provider", "local") == "claude":
        return cfg.claude_model
    return cfg.llm_model if korean else (getattr(cfg, "llm_foreign_model", "") or cfg.llm_model)


def main_model(cfg) -> str:
    return model_for(cfg, True)
