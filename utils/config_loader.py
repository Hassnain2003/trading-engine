"""Central config loader for the Crypto Agent.

Loads config.yaml and applies environment-variable overrides using the
SECTION__KEY convention (e.g. RISK__DAILY_LOSS_LOCK_PCT=0.03).

Secrets (API keys, Gemini key, ntfy topic) are NOT stored here — they stay
in .env and are read by their respective modules (claude/config.py, etc.).
"""

import os
from copy import deepcopy

import yaml

_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config.yaml')
_cache = None


def _env_override(cfg: dict) -> dict:
    """Apply env overrides like RISK__DAILY_LOSS_LOCK_PCT=0.03 into cfg['risk']['daily_loss_lock_pct']."""
    result = deepcopy(cfg)
    for env_key, env_val in os.environ.items():
        if '__' not in env_key:
            continue
        section, key = env_key.split('__', 1)
        section = section.lower()
        key = key.lower()
        try:
            # Try to coerce to the existing type
            if section in result and key in result[section]:
                existing = result[section][key]
                if isinstance(existing, bool):
                    result[section][key] = env_val.lower() in ('1', 'true', 'yes', 'on')
                elif isinstance(existing, int):
                    result[section][key] = int(env_val)
                elif isinstance(existing, float):
                    result[section][key] = float(env_val)
                else:
                    result[section][key] = env_val
            else:
                # New key — store as string unless it looks like a number
                try:
                    result.setdefault(section, {})[key] = float(env_val)
                except ValueError:
                    result.setdefault(section, {})[key] = env_val
        except (ValueError, TypeError):
            pass
    return result


def load(path: str | None = None) -> dict:
    """Load config.yaml (with env overrides) and return a nested dict."""
    global _cache
    if _cache is not None:
        return deepcopy(_cache)

    path = path or _CONFIG_PATH
    with open(path, 'r') as f:
        cfg = yaml.safe_load(f) or {}

    cfg = _env_override(cfg)
    _cache = cfg
    return deepcopy(cfg)


def get(section: str, key: str, default=None):
    """Convenience: config_loader.get('risk', 'daily_loss_lock_pct', 0.02)."""
    cfg = load()
    return cfg.get(section, {}).get(key, default)


def apply_env_overrides(cfg: dict) -> dict:
    """Public env-override pass for callers that merge config data themselves.

    risk_manager builds its effective per-strategy config from config.yaml plus
    trading_phase constants; the phase constants are Python data, not config,
    so SECTION__KEY env overrides must be re-applied AFTER the merge or an env
    var would be silently clobbered by the phase dict.
    """
    return _env_override(cfg)


def reset():
    """Clear the cache (useful in tests)."""
    global _cache
    _cache = None