import logging
import time

from google import genai
from google.genai import errors
from .config import GEMINI_API_KEY, GEMINI_MODEL

_client = None


def _get_client():
    """Create the Gemini client lazily so importing this module never
    requires a real API key (safe-by-default: a clone without .env must
    still import cleanly)."""
    global _client
    if _client is None:
        _client = genai.Client(api_key=GEMINI_API_KEY)
    return _client

RETRYABLE_CODES = {429, 500, 503}
MAX_ATTEMPTS = 3
BACKOFF_BASE_SEC = 2

def ask_gemini(system_prompt, user_prompt, max_tokens=1024, thinking_budget=None):
    """thinking_budget=0 disables gemini-2.5's internal reasoning — needed for
    structured-output tasks because thinking tokens count against
    max_output_tokens and can truncate the actual answer mid-JSON."""
    full_prompt = f"[System instructions]: {system_prompt}\n\n[User message]: {user_prompt}"
    config_kwargs = dict(max_output_tokens=max_tokens, temperature=0.3)
    if thinking_budget is not None:
        config_kwargs['thinking_config'] = genai.types.ThinkingConfig(
            thinking_budget=thinking_budget
        )
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = _get_client().models.generate_content(
                model=GEMINI_MODEL,
                contents=full_prompt,
                config=genai.types.GenerateContentConfig(**config_kwargs),
            )
            return response.text
        except errors.APIError as e:
            if e.code not in RETRYABLE_CODES or attempt == MAX_ATTEMPTS:
                raise
            time.sleep(BACKOFF_BASE_SEC * attempt)


logger = logging.getLogger('gemini_client')


def ask_gemini_or_fallback(system_prompt, user_prompt, fallback, **kwargs):
    """Gemini (free tier) 503s often and has no paid fallback. The monitoring
    jobs are advisory, so on ANY failure log it and return the caller's
    deterministic `fallback` text instead of raising — the job still produces
    output rather than silently going dark."""
    try:
        return ask_gemini(system_prompt, user_prompt, **kwargs)
    except Exception as e:
        logger.warning(f"Gemini unavailable ({e}); using rule-based fallback")
        return fallback
