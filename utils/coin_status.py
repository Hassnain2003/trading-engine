"""Parse Gemini pre-session JSON into per-symbol trading status."""

import json
import re


def parse_pre_session_status(response, symbols):
    """
    Returns (disabled_symbols, reduced_symbols) sets.
    Expects JSON like {"BTC": {"status": "ACTIVE", ...}, ...} in the response.
    """
    disabled = set()
    reduced = set()
    if not response:
        return disabled, reduced

    text = response.strip()
    fence = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        start, end = text.find('{'), text.rfind('}')
        if start >= 0 and end > start:
            text = text[start:end + 1]

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return disabled, reduced

    coin_status = {}
    for key, val in data.items():
        if key.lower() == 'summary':
            continue
        if isinstance(val, dict):
            coin_status[key.upper()] = str(val.get('status', '')).upper()
        elif isinstance(val, str):
            coin_status[key.upper()] = val.upper()

    for symbol in symbols:
        coin = symbol.split('/')[0].upper()
        status = coin_status.get(coin, 'ACTIVE')
        if status == 'DISABLED':
            disabled.add(symbol)
        elif status == 'REDUCED':
            reduced.add(symbol)

    return disabled, reduced
