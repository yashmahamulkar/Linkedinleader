import os
import json
from pathlib import Path
from typing import Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent

class PreferenceManager:
    """Load dynamic preferences (roles, locations, custom categories) from a JSON file.

    Expected file structure (preferences.json):
    {
      "preferred_roles": ["Software Developer", "AI/ML Engineer"],
      "preferred_locations": ["Remote", "Mumbai"],
      "custom_instructions": "",
      "categories": {
        "software_dev": {
          "display_name": "Software Developer",
          "rules": "Traditional software engineering, web development (frontend/backend/fullstack).",
          "email_subject": "Application for {job_title} at {company_name}"
        },
        "ai": {
          "display_name": "AI/ML Engineer",
          "rules": "Artificial intelligence, Machine Learning, Deep Learning, Generative AI.",
          "email_subject": "AI Role Application - {job_title} at {company_name}"
        }
      }
    }
    """

    def __init__(self, preferences_path: Optional[str] = None, user_id: Optional[str] = None):
        self.user_id = user_id
        if user_id:
            self.user_dir = PROJECT_ROOT / "users" / user_id
            self.preferences_path = str(self.user_dir / "preferences.json")
            os.makedirs(os.path.dirname(self.preferences_path), exist_ok=True)
        else:
            self.user_dir = None
            if preferences_path:
                self.preferences_path = preferences_path
            else:
                self.preferences_path = str(PROJECT_ROOT / "configs" / "preferences.json")
                
        self._data = self._load_preferences()

    def _load_preferences(self) -> Dict:
        if os.path.exists(self.preferences_path):
            try:
                with open(self.preferences_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                return {
                    'preferred_roles': data.get('preferred_roles', ["Software Developer", "AI/ML Engineer"]),
                    'preferred_locations': data.get('preferred_locations', ["Remote", "Mumbai"]),
                    'custom_instructions': data.get('custom_instructions', ""),
                    'categories': data.get('categories', self._default_categories())
                }
            except Exception:
                return self._default_preference_data()
        else:
            default_data = self._default_preference_data()
            if self.user_id:
                self._data = default_data
                self.save_preferences()
            return default_data

    def _default_preference_data(self) -> Dict:
        return {
            'preferred_roles': ["Software Developer", "AI/ML Engineer"],
            'preferred_locations': ["Remote", "Mumbai"],
            'custom_instructions': "",
            'categories': self._default_categories()
        }

    def _default_categories(self) -> Dict:
        return {
            "software_dev": {
                "display_name": "Software Developer",
                "rules": "Traditional software engineering, web development (frontend/backend/fullstack), using Python, Javascript, React, Java, C++, etc.",
                "email_subject": "Application for {job_title} at {company_name}"
            },
            "ai": {
                "display_name": "AI/ML Engineer",
                "rules": "Artificial intelligence, Machine Learning, Deep Learning, Generative AI, PyTorch, TensorFlow, NLP, LLMs.",
                "email_subject": "AI/ML Role Application - {job_title} at {company_name}"
            }
        }

    def preferred_roles(self) -> List[str]:
        return list(self._data.get('preferred_roles', []))

    def preferred_locations(self) -> List[str]:
        return list(self._data.get('preferred_locations', []))

    def custom_instructions(self) -> str:
        return str(self._data.get('custom_instructions', ""))

    def categories(self) -> Dict:
        return dict(self._data.get('categories', self._default_categories()))

    def save_preferences(self):
        try:
            os.makedirs(os.path.dirname(self.preferences_path), exist_ok=True)
            with open(self.preferences_path, 'w', encoding='utf-8') as f:
                json.dump(self._data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"Error saving preferences: {e}")

    # Backwards compatible helpers
    def common_tech_roles(self) -> List[str]:
        return self.preferred_roles()

    def internship_roles(self) -> List[str]:
        return []

    def common_locations(self) -> List[str]:
        return self.preferred_locations()