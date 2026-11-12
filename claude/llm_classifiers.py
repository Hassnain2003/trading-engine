"""Pluggable LLM news classifiers for the S8 A/B test.

Same prompt, same headlines, different models — so we can settle whether
Gemini's ~50% news-direction hit rate is the model or the task. Each
classifier returns a list of {index, coin, direction, severity, reason}.

Gemini: existing free tier (google-genai). Groq: OpenAI-compatible free
tier (llama-3.3-70b) via plain requests — no extra dependency, activates
automatically when GROQ_API_KEY is set in .env.
"""

import json
import os
import re
import logging

import requests

logger = logging.getLogger('llm_classifiers')

CLASSIFY_SYSTEM = (
    "You are a crypto news analyst. For each numbered headline, judge its likely "
    "price impact on the bracketed coin(s) over the next 24 hours. Direction must be "
    "bullish, bearish, or neutral. Severity 1 (barely matters) to 5 (major market event). "
    "Be skeptical: most news is neutral noise — reserve non-neutral calls for genuinely "
    "price-relevant events (hacks, ETF flows, regulation, listings, protocol failures)."
)


def _build_prompt(headlines):
    numbered = "\n".join(f"{i}. [{h['coins']}] {h['title']}" for i, h in enumerate(headlines))
    return (
        f"Headlines:\n{numbered}\n\n"
        'Reply with ONLY a JSON array like: '
        '[{"index": 0, "coin": "BTC", "direction": "bearish", "severity": 3, "reason": "..."}]. '
        "One entry per headline per affected coin. Use neutral/1 when in doubt."
    )


def _extract_json_array(text):
    text = (text or '').strip()
    fence = re.search(r'```(?:json)?\s*(\[.*?\])\s*```', text, re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        start, end = text.find('['), text.rfind(']')
        if start >= 0 and end > start:
            text = text[start:end + 1]
        elif start >= 0:
            text = text[start:]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        last = text.rfind('}')
        if last > 0:
            try:
                return json.loads(text[:last + 1] + ']')
            except json.JSONDecodeError:
                pass
    logger.warning("Classifier: unparseable response")
    return []


def classify_gemini(headlines):
    from .gemini_client import ask_gemini
    resp = ask_gemini(CLASSIFY_SYSTEM, _build_prompt(headlines), max_tokens=3000, thinking_budget=0)
    return _extract_json_array(resp)


GROQ_MODEL = os.getenv('GROQ_MODEL', 'llama-3.3-70b-versatile')


def groq_available():
    return bool(os.getenv('GROQ_API_KEY', '').strip())


def classify_groq(headlines):
    key = os.getenv('GROQ_API_KEY', '').strip()
    if not key:
        return []
    resp = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "model": GROQ_MODEL,
            "messages": [
                {"role": "system", "content": CLASSIFY_SYSTEM},
                {"role": "user", "content": _build_prompt(headlines)},
            ],
            "temperature": 0.3,
            "max_tokens": 3000,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        logger.warning(f"Groq HTTP {resp.status_code}: {resp.text[:150]}")
        return []
    content = resp.json()['choices'][0]['message']['content']
    return _extract_json_array(content)


OPENROUTER_MODEL = os.getenv('OPENROUTER_MODEL', 'google/gemma-4-26b-a4b-it:free')


def openrouter_available():
    return bool(os.getenv('OPENROUTER_API_KEY', '').strip())


def classify_openrouter(headlines):
    key = os.getenv('OPENROUTER_API_KEY', '').strip()
    if not key:
        return []
    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "model": OPENROUTER_MODEL,
            "messages": [
                {"role": "system", "content": CLASSIFY_SYSTEM},
                {"role": "user", "content": _build_prompt(headlines)},
            ],
            "temperature": 0.3,
            "max_tokens": 3000,
        },
        timeout=40,
    )
    if resp.status_code != 200:
        # free tier 429s often — skip quietly, it's a bonus contender
        logger.info(f"OpenRouter HTTP {resp.status_code} (skipping batch)")
        return []
    content = resp.json()['choices'][0]['message']['content']
    return _extract_json_array(content)


# Registry of models participating in the A/B (only ones actually configured).
def active_models():
    models = [('gemini', classify_gemini)]
    if groq_available():
        models.append(('groq', classify_groq))
    if openrouter_available():
        models.append(('openrouter', classify_openrouter))
    return models
