from typing import Dict, List, Optional, Union, Tuple
import json
import os
import logging
from dotenv import load_dotenv

load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class ConfigManager:    
    def __init__(self, config_path: str = "./configs/config.json"):
        self.config_path = config_path
        self.config = self._load_config()
    
    def _load_config(self) -> Dict:
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logging.warning(f"Error loading config from {self.config_path}: {e}")
                return self._default_config()
        else:
            logging.info(f"Config file {self.config_path} not found, using defaults")
            return self._default_config()
    
    def _default_config(self) -> Dict:
        return {
            "auto_email": False,
            "scraper_keys_path": "scraper_keys.json",
            "gemini_keys_path": "gemini_keys.json",
            "max_posts_per_key": 250,
            "enable_parallel_extraction": True,
            "log_level": "INFO"
        }
    
    def get(self, key: str, default=None):
        return self.config.get(key, default)
    
    def is_auto_email_enabled(self) -> bool:
        return self.config.get("auto_email", False)
    
    def save_config(self):
        """Save current config to file"""
        try:
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=2, ensure_ascii=False)
            logging.info(f"Config saved to {self.config_path}")
        except Exception as e:
            logging.error(f"Failed to save config: {e}")
            raise
