
import os
import json
from typing import Dict,List

class PreferenceManager:
    """Load dynamic preferences (roles, locations) from a JSON file.

    Expected file structure (preferences.json):
    {
      "preferred_roles": ["Software Developer", "AI/ML Engineer"],
      "preferred_locations": ["Remote", "Bangalore"]
    }
    """

    def __init__(self, preferences_path: str = "./configs/preferences.json"):
        self.preferences_path = preferences_path
        self._data = self._load_preferences()

    def _load_preferences(self) -> Dict[str, List[str]]:
        if os.path.exists(self.preferences_path):
            try:
                with open(self.preferences_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                return {
                    'preferred_roles': data.get('preferred_roles', ["Software Developer", "AI/ML Engineer",]),
                    'preferred_locations': data.get('preferred_locations', ["Remote", "Mumbai"])
                }
            except Exception:
                return {'preferred_roles': [], 'preferred_locations': []}
        return {'preferred_roles': [], 'preferred_locations': []}

    def preferred_roles(self) -> List[str]:
        return list(self._data.get('preferred_roles', []))

    def preferred_locations(self) -> List[str]:
        return list(self._data.get('preferred_locations', []))

    # Backwards compatible helpers that the existing main() used
    def common_tech_roles(self) -> List[str]:
        return self.preferred_roles()

    def internship_roles(self) -> List[str]:
        return []

    def common_locations(self) -> List[str]:
        return self.preferred_locations()