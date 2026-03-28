"""
Sports market detection for filtering non-sports markets.

US API only supports sports markets, so this module provides fast
slug-based detection to avoid unnecessary API calls for politics,
crypto, and other market types.
"""
from typing import Pattern
import re


# Sports league/event patterns
SPORTS_PATTERNS: list[Pattern] = [
    # Major US leagues
    re.compile(r'\b(nba|nfl|nhl|mlb|mls)\b', re.IGNORECASE),
    
    # College sports
    re.compile(r'\b(ncaa|ncaab|ncaaf)\b', re.IGNORECASE),
    
    # International soccer
    re.compile(r'\b(fifa|uefa|epl|premier-league|champions-league|world-cup)\b', re.IGNORECASE),
    
    # Other sports
    re.compile(r'\b(ufc|mma|boxing|tennis|golf|f1|formula-1)\b', re.IGNORECASE),
    
    # Team name patterns (common indicators)
    re.compile(r'-(vs?-|at-)', re.IGNORECASE),  # team-vs-team format
    
    # Date patterns in slugs (common for sports)
    re.compile(r'-\d{4}-\d{2}-\d{2}', re.IGNORECASE),  # YYYY-MM-DD
]


def is_likely_sports_market(slug: str) -> bool:
    """
    Fast slug-based detection of sports markets.
    
    Uses pattern matching to identify sports-related slugs without
    making API calls. This pre-filters non-sports markets (politics,
    crypto, etc.) which the US API doesn't support.
    
    Args:
        slug: Market slug to check
        
    Returns:
        True if slug appears to be sports-related, False otherwise
    """
    if not slug:
        return False
    
    slug_lower = slug.lower()
    
    # Check against all sports patterns
    for pattern in SPORTS_PATTERNS:
        if pattern.search(slug_lower):
            return True
    
    return False


def is_sports_market(slug: str) -> bool:
    """Backward-compatible alias used by main entry points."""
    return is_likely_sports_market(slug)
