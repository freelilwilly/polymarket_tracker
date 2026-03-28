"""
Slug resolution and conversion for Polymarket markets.

Handles conversion between EU trader slugs and US API slugs,
with learning and persistence for successful mappings.
"""
import json
import logging
from pathlib import Path
from typing import Optional

from config import Config

logger = logging.getLogger(__name__)


class SlugConverter:
    """Converts and caches slug mappings between EU and US APIs."""
    
    def __init__(self):
        """Initialize slug converter with learned mappings."""
        self.learned_mappings: dict[str, str] = {}
        self.mapping_file = Path(Config.SLUG_LEARNED_MAPPINGS_FILE)
        
        # Load previously learned mappings
        if self.mapping_file.exists():
            try:
                with open(self.mapping_file, "r", encoding="utf-8") as f:
                    self.learned_mappings = json.load(f)
                logger.info(f"Loaded {len(self.learned_mappings)} learned slug mappings")
            except Exception as e:
                logger.warning(f"Could not load learned mappings: {e}")
    
    def learn_mapping(self, eu_slug: str, us_slug: str) -> None:
        """
        Learn a successful slug mapping for future use.
        
        Args:
            eu_slug: European/international slug from trader
            us_slug: US API slug that worked
        """
        if not Config.SLUG_PERSIST_LEARNED_MAPPINGS:
            return
        
        eu_normalized = self._normalize(eu_slug)
        us_normalized = self._normalize(us_slug)
        
        if eu_normalized and us_normalized:
            self.learned_mappings[eu_normalized] = us_normalized
            self._save_mappings()
            logger.debug(f"Learned mapping: {eu_normalized} -> {us_normalized}")
    
    def get_learned_mapping(self, eu_slug: str) -> Optional[str]:
        """
        Get a previously learned US slug for an EU slug.
        
        Args:
            eu_slug: European/international slug from trader
            
        Returns:
            US slug if learned, None otherwise
        """
        eu_normalized = self._normalize(eu_slug)
        return self.learned_mappings.get(eu_normalized)
    
    @staticmethod
    def _normalize(slug: str) -> str:
        """Normalize slug for consistent comparison."""
        normalized = str(slug or "").strip().lower()
        if normalized.startswith("aec-"):
            normalized = normalized[4:]
        return normalized
    
    def _save_mappings(self) -> None:
        """Persist learned mappings to file."""
        if not Config.SLUG_PERSIST_LEARNED_MAPPINGS:
            return
        
        try:
            # Atomic write pattern
            temp_path = self.mapping_file.with_suffix(self.mapping_file.suffix + ".tmp")
            with temp_path.open("w", encoding="utf-8") as handle:
                json.dump(self.learned_mappings, handle, indent=2, ensure_ascii=False)
            temp_path.replace(self.mapping_file)
        except Exception as e:
            logger.warning(f"Could not save learned mappings: {e}")

    def save_mappings(self) -> None:
        """Public persistence entrypoint used by shutdown handlers."""
        self._save_mappings()
