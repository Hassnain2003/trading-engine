"""S8 candidate — news-driven shadow signals. NO REAL ORDERS.

Pipeline: crypto RSS headlines → Gemini classifies each into
{coin, direction, severity, reason} → recorded in shadow_signals with the
price at signal time → forward prices (1h/4h/24h) filled as they mature.

After a few weeks, scripts/shadow_report.py answers: do Gemini's news calls
actually predict price? Only a validated YES earns this pipeline execution
rights as S8 — same gate every strategy passes.
"""

import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests

from .gemini_client import ask_gemini

logger = logging.getLogger('news_shadow')

FEEDS = [
    ('cointelegraph', 'https://cointelegraph.com/rss'),
    ('coindesk', 'https://www.coindesk.com/arc/outboundfeeds/rss/'),
    ('decrypt', 'https://decrypt.co/feed'),
    ('bitcoinmagazine', 'https://bitcoinmagazine.com/feed'),
]

COIN_KEYWORDS = {
    'BTC': ['bitcoin', 'btc'],
    'ETH': ['ethereum', 'eth ', 'ether '],
    'SOL': ['solana', ' sol '],
    'BNB': ['bnb', 'binance coin'],
    'XRP': ['xrp', 'ripple'],
    'LINK': ['chainlink', ' link '],
    'DOGE': ['dogecoin', 'doge'],
}

COIN_TO_SYMBOL = {c: f"{c}/USDT:USDT" for c in COIN_KEYWORDS}

FETCH_INTERVAL_SEC = 30 * 60
MAX_HEADLINES_PER_BATCH = 8
_last_fetch = 0.0


def _fetch_feed(source, url):
    try:
        resp = requests.get(url, timeout=15, allow_redirects=True,
                            headers={'User-Agent': 'Mozilla/5.0 (crypto-agent-rss)'})
        if resp.status_code != 200:
            return []
        root = ET.fromstring(resp.content)
        items = []
        for item in root.iter('item'):
            title = (item.findtext('title') or '').strip()
            link = (item.findtext('link') or '').strip()
            if title and link:
                items.append({'source': source, 'title': title, 'link': link})
        return items[:30]
    except Exception as e:
        logger.warning(f"Feed {source} failed: {e}")
        return []


def _relevant_coins(title):
    t = f" {title.lower()} "
    return [coin for coin, kws in COIN_KEYWORDS.items() if any(kw in t for kw in kws)]


def _classify(headlines):
    """One batched Gemini call → list of {index, coin, direction, severity, reason}."""
    numbered = "\n".join(f"{i}. [{h['coins']}] {h['title']}" for i, h in enumerate(headlines))
    system = (
        "You are a crypto news analyst. For each numbered headline, judge its likely "
        "price impact on the bracketed coin(s) over the next 24 hours. Direction must be "
        "bullish, bearish, or neutral. Severity 1 (barely matters) to 5 (major market event). "
        "Be skeptical: most news is neutral noise — reserve non-neutral calls for genuinely "
        "price-relevant events (hacks, ETF flows, regulation, listings, protocol failures)."
    )
    prompt = (
        f"Headlines:\n{numbered}\n\n"
        'Reply with ONLY a JSON array like: '
        '[{"index": 0, "coin": "BTC", "direction": "bearish", "severity": 3, "reason": "..."}]. '
        "One entry per headline per affected coin. Use neutral/1 when in doubt."
    )
    # thinking_budget=0: gemini-2.5's reasoning tokens otherwise eat the
    # output budget and truncate the JSON mid-array.
    response = ask_gemini(system, prompt, max_tokens=3000, thinking_budget=0)
    text = (response or '').strip()
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
        # Truncation repair: keep complete objects, drop the cut-off tail
        last = text.rfind('}')
        if last > 0:
            try:
                return json.loads(text[:last + 1] + ']')
            except json.JSONDecodeError:
                pass
        logger.warning("Shadow classify: unparseable Gemini response")
        return []


def run_news_shadow(conn, get_price):
    """Called every maintenance cycle; internally throttled.

    get_price(symbol) -> float or None (public ticker last).
    """
    _fill_forward_prices(conn, get_price)

    global _last_fetch
    now = time.time()
    if now - _last_fetch < FETCH_INTERVAL_SEC:
        return
    _last_fetch = now

    candidates = []
    for source, url in FEEDS:
        for item in _fetch_feed(source, url):
            coins = _relevant_coins(item['title'])
            if not coins:
                continue
            seen = conn.execute(
                "SELECT 1 FROM shadow_signals WHERE link=? LIMIT 1", (item['link'],)
            ).fetchone()
            if seen:
                continue
            item['coins'] = ",".join(coins)
            candidates.append(item)

    if not candidates:
        return
    batch = candidates[:MAX_HEADLINES_PER_BATCH]
    logger.info(f"News shadow: classifying {len(batch)} new headline(s)")
    try:
        results = _classify(batch)
    except Exception as e:
        logger.warning(f"News classification failed: {e}")
        return

    ts = datetime.now(timezone.utc).isoformat()
    inserted = 0
    for r in results:
        try:
            idx = int(r.get('index'))
            h = batch[idx]
            coin = str(r.get('coin', '')).upper()
            if coin not in COIN_TO_SYMBOL:
                continue
            price = get_price(COIN_TO_SYMBOL[coin])
            conn.execute(
                """INSERT OR IGNORE INTO shadow_signals
                   (timestamp, source, link, headline, coin, direction, severity, reason, price_at_signal)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (ts, h['source'], h['link'], h['title'], coin,
                 str(r.get('direction', 'neutral')).lower(),
                 int(r.get('severity', 1)), str(r.get('reason', ''))[:300], price),
            )
            inserted += 1
        except Exception:
            continue
    conn.commit()
    if inserted:
        logger.info(f"News shadow: recorded {inserted} signal(s)")

    _run_ab_classification(conn, batch, get_price, ts)


def _run_ab_classification(conn, batch, get_price, ts):
    """A/B: classify the SAME batch with every configured model (Gemini +
    Groq if keyed) into ab_news_signals, tagged by model, for a head-to-head
    fair comparison over identical headlines. Only runs models not already
    recorded for these links."""
    from claude.llm_classifiers import active_models
    models = active_models()
    if len(models) < 2:
        return  # nothing to compare against Gemini yet (no GROQ_API_KEY)

    for model_name, classify_fn in models:
        # skip if this model already classified this exact batch (dedup by first link)
        already = conn.execute(
            "SELECT 1 FROM ab_news_signals WHERE model=? AND link=? LIMIT 1",
            (model_name, batch[0]['link']),
        ).fetchone()
        if already:
            continue
        try:
            results = classify_fn(batch)
        except Exception as e:
            logger.warning(f"A/B {model_name} classification failed: {e}")
            continue
        for r in results:
            try:
                idx = int(r.get('index'))
                h = batch[idx]
                coin = str(r.get('coin', '')).upper()
                if coin not in COIN_TO_SYMBOL:
                    continue
                price = get_price(COIN_TO_SYMBOL[coin])
                conn.execute(
                    """INSERT OR IGNORE INTO ab_news_signals
                       (timestamp, model, link, headline, coin, direction, severity, reason, price_at_signal)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (ts, model_name, h['link'], h['title'], coin,
                     str(r.get('direction', 'neutral')).lower(),
                     int(r.get('severity', 1)), str(r.get('reason', ''))[:300], price),
                )
            except Exception:
                continue
    conn.commit()


def _fill_forward_prices(conn, get_price):
    """Fill 1h/4h/24h forward prices on matured shadow signals (cheap query)."""
    now = datetime.now(timezone.utc)
    for col, hours in [('price_1h', 1), ('price_4h', 4), ('price_24h', 24)]:
        rows = conn.execute(
            f"SELECT id, coin, timestamp FROM shadow_signals WHERE {col} IS NULL "
            f"AND price_at_signal IS NOT NULL LIMIT 20"
        ).fetchall()
        for sid, coin, ts in rows:
            try:
                placed = datetime.fromisoformat(ts)
                if (now - placed).total_seconds() < hours * 3600:
                    continue
                price = get_price(COIN_TO_SYMBOL.get(coin, ''))
                if price:
                    conn.execute(f"UPDATE shadow_signals SET {col}=? WHERE id=?", (price, sid))
            except Exception:
                continue
        # same forward-price fill for the A/B table
        ab_rows = conn.execute(
            f"SELECT id, coin, timestamp FROM ab_news_signals WHERE {col} IS NULL "
            f"AND price_at_signal IS NOT NULL LIMIT 30"
        ).fetchall()
        for sid, coin, ts in ab_rows:
            try:
                placed = datetime.fromisoformat(ts)
                if (now - placed).total_seconds() < hours * 3600:
                    continue
                price = get_price(COIN_TO_SYMBOL.get(coin, ''))
                if price:
                    conn.execute(f"UPDATE ab_news_signals SET {col}=? WHERE id=?", (price, sid))
            except Exception:
                continue
    conn.commit()
