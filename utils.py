"""Utility functions for the copy trade bot."""
import difflib
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional


def setup_logging(log_file: str, log_level: str = "INFO") -> logging.Logger:
    """Set up logging configuration."""
    level = getattr(logging, log_level.upper(), logging.INFO)
    
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(),
        ],
    )
    
    return logging.getLogger(__name__)


def to_float(value: Any, default: float = 0.0) -> float:
    """Safely convert value to float."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def to_timestamp_iso(value: Any) -> str:
    """Convert timestamp to ISO format."""
    try:
        ts = float(value)
        if ts > 1_000_000_000_000:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return ""


def median(values: list[float]) -> float:
    """Calculate median of a list of floats."""
    if not values:
        return 0.0
    ordered = sorted(values)
    size = len(ordered)
    midpoint = size // 2
    if size % 2 == 1:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / 2.0


def calculate_percentile(history: list[float], value: float) -> float:
    """Calculate percentile of a value within a history."""
    if not history:
        return 0.5
    
    count_leq = sum(1 for item in history if item <= value)
    percentile = count_leq / len(history)
    return max(0.0, min(1.0, percentile))


def calculate_multiplier(
    percentile: float,
    observed_size: float,
    wallet_median_size: float,
    min_multiplier: float,
    max_multiplier: float,
    curve_power: float,
    low_size_threshold_ratio: float,
    low_size_haircut_power: float,
    low_size_haircut_min_factor: float,
) -> float:
    """
    Calculate position size multiplier based on percentile and size ratios.
    This matches the normalization logic from polymarket_tracker.
    """
    percentile = max(0.0, min(1.0, percentile))
    base_multiplier = min_multiplier + (max_multiplier - min_multiplier) * (percentile ** curve_power)
    
    adjusted_multiplier = base_multiplier
    if wallet_median_size > 0 and observed_size > 0 and low_size_threshold_ratio > 0:
        relative_size = observed_size / wallet_median_size
        if relative_size < low_size_threshold_ratio:
            haircut = max(low_size_haircut_min_factor, relative_size ** low_size_haircut_power)
            adjusted_multiplier *= haircut
    
    max_allowed = max(max_multiplier, min_multiplier)
    return max(0.0, min(adjusted_multiplier, max_allowed))


def trade_key(trade: dict[str, Any]) -> str:
    """Generate unique key for a trade."""
    trade_id = trade.get("id")
    if trade_id is not None:
        return str(trade_id)
    
    tx_hash = trade.get("transactionHash") or ""
    timestamp = trade.get("timestamp") or ""
    asset = trade.get("asset") or ""
    outcome = trade.get("outcome") or ""
    side = trade.get("side") or ""
    size = trade.get("size") or ""
    price = trade.get("price") or ""
    return f"{tx_hash}:{timestamp}:{asset}:{outcome}:{side}:{size}:{price}"


def normalize_slug_for_key(slug: Any) -> str:
    """Normalize slug for keying: lowercase and strip optional aec- prefix."""
    normalized = str(slug or "").strip().lower()
    if normalized.startswith("aec-"):
        normalized = normalized[4:]
    return normalized


def normalize_market_key(market_key: Any) -> str:
    """Normalize market key to '<normalized_slug>|<lower_outcome>' format."""
    text = str(market_key or "").strip().lower()
    if "|" not in text:
        return normalize_slug_for_key(text)

    slug_part, outcome_part = text.split("|", 1)
    return f"{normalize_slug_for_key(slug_part)}|{outcome_part.strip().lower()}"


def canonical_market_key(slug: Any, outcome: Any) -> str:
    """Build canonical market key from slug/outcome."""
    return f"{normalize_slug_for_key(slug)}|{str(outcome or '').strip().lower()}"


def canonical_position_key(wallet: Any, slug: Any, outcome: Any) -> str:
    """Build canonical position key from wallet + canonical market key."""
    wallet_part = str(wallet or "").strip().lower()
    return f"{wallet_part}|{canonical_market_key(slug, outcome)}"


def instrument_parts(trade: dict[str, Any]) -> dict[str, str]:
    """Extract instrument identification parts from a trade."""
    market = str(
        trade.get("title") or 
        trade.get("slug") or 
        trade.get("eventSlug") or 
        "Unknown Market"
    )
    outcome = str(trade.get("outcome") or "Unknown")
    
    base_id = str(
        trade.get("asset") or 
        trade.get("slug") or 
        trade.get("eventSlug") or 
        trade.get("conditionId") or 
        trade.get("title") or 
        "unknown"
    )
    base_id = base_id.strip().lower().replace(" ", "_")
    
    slug = str(trade.get("slug") or base_id)
    market_key = canonical_market_key(slug, outcome)
    
    return {
        "market": market,
        "outcome": outcome,
        "base_id": base_id,
        "market_key": market_key,
        "slug": slug,
    }


YES_ALIASES = {"yes", "y", "true", "1", "long", "buy_long", "over"}
NO_ALIASES = {"no", "n", "false", "0", "short", "buy_short", "under"}


def canonicalize_outcome_text(text: Any) -> str:
    """Normalize outcome text for robust matching across formatting differences."""
    raw = str(text or "").strip().lower()
    if not raw:
        return ""
    cleaned = re.sub(r"[^a-z0-9]+", "", raw)
    return cleaned


def _tokenize_outcome_text(text: Any) -> list[str]:
    raw = str(text or "").strip().lower()
    if not raw:
        return []
    tokens = re.findall(r"[a-z0-9]+", raw)
    return tokens


def resolve_outcome_index(
    outcome: str,
    tokens: list[dict[str, Any]],
    allow_token_overlap: bool = True,
    allow_fuzzy: bool = True,
) -> Optional[int]:
    """Resolve requested outcome to token index using strict deterministic matching tiers."""
    if not tokens:
        return None

    outcome_raw = str(outcome or "").strip()
    outcome_lower = outcome_raw.lower()
    outcome_canon = canonicalize_outcome_text(outcome_raw)

    # Binary YES/NO resolution (tier 0)
    if len(tokens) == 2:
        if outcome_lower in YES_ALIASES:
            return 0
        if outcome_lower in NO_ALIASES:
            return 1

    # Tier 1: Exact case-insensitive match
    for idx, token in enumerate(tokens):
        token_outcome = str(token.get("outcome") or "").strip()
        if token_outcome.lower() == outcome_lower:
            return idx

    # Tier 2: Canonical match (alphanumeric only)
    token_canons = [canonicalize_outcome_text(t.get("outcome")) for t in tokens]
    for idx, token_canon in enumerate(token_canons):
        if token_canon and token_canon == outcome_canon:
            return idx

    # Tier 3: Token overlap (for complex team names)
    if allow_token_overlap:
        outcome_tokens = set(_tokenize_outcome_text(outcome_raw))
        if outcome_tokens:
            # Prefer exact token containment if one token label is fully contained
            # in the incoming outcome phrase and unambiguous.
            containment_hits: list[int] = []
            for idx, token in enumerate(tokens):
                token_set = set(_tokenize_outcome_text(token.get("outcome") or ""))
                if token_set and token_set.issubset(outcome_tokens):
                    containment_hits.append(idx)
            if len(containment_hits) == 1:
                return containment_hits[0]

            overlaps: list[tuple[float, int]] = []
            for idx, token in enumerate(tokens):
                token_text = token.get("outcome") or ""
                token_set = set(_tokenize_outcome_text(token_text))
                if not token_set:
                    continue
                overlap = len(outcome_tokens & token_set) / len(outcome_tokens | token_set)
                overlaps.append((overlap, idx))

            overlaps.sort(reverse=True)
            if len(overlaps) >= 2:
                best_overlap, best_idx = overlaps[0]
                second_overlap = overlaps[1][0]
                if best_overlap >= 0.6 and (best_overlap - second_overlap) >= 0.15:
                    return best_idx

    # Tier 4: Fuzzy similarity
    if allow_fuzzy:
        similarity_scores: list[tuple[float, int]] = []
        for idx, canon in enumerate(token_canons):
            if not canon:
                continue
            ratio = difflib.SequenceMatcher(a=outcome_canon, b=canon).ratio()
            similarity_scores.append((ratio, idx))

        similarity_scores.sort(reverse=True)
        if similarity_scores:
            best_ratio, best_idx = similarity_scores[0]
            second_ratio = similarity_scores[1][0] if len(similarity_scores) > 1 else 0.0
            if best_ratio >= 0.86 and (best_ratio - second_ratio) >= 0.08:
                return best_idx

    return None


async def normalize_outcome_to_yes_no(
    api_client,
    slug: str,
    outcome: str,
    logger,
    strict: bool = False,
    allow_fuzzy: Optional[bool] = None,
) -> Optional[str]:
    """
    Normalize arbitrary outcome text to 'yes' or 'no' for binary markets.
    
    Returns:
        'yes' or 'no' if outcome is unambiguously resolved, None otherwise
    """
    try:
        market_info = await api_client.get_market_info(slug)
        if not market_info:
            logger.warning(f"Cannot get market info for slug: {slug}")
            return None

        tokens = market_info.get("tokens") or []
        if len(tokens) != 2:
            logger.warning(f"Market {slug} is not binary (has {len(tokens)} tokens)")
            return None

        fuzzy_enabled = (not strict) if allow_fuzzy is None else bool(allow_fuzzy)

        resolved_index = resolve_outcome_index(
            outcome,
            tokens,
            allow_token_overlap=not strict,
            allow_fuzzy=fuzzy_enabled,
        )
        if resolved_index is None:
            logger.error(f"Could not resolve outcome '{outcome}' for {slug}")
            logger.error(f"Available outcomes: {[t.get('outcome') for t in tokens]}")
            return None

        return "yes" if resolved_index == 0 else "no"

    except Exception as e:
        logger.error(f"Error normalizing outcome for {slug}: {e}")
        return None
