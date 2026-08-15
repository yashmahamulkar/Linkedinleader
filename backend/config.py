from typing import Dict, List, Optional, Union, Tuple
import json
import os
import logging
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

PROJECT_ROOT = Path(__file__).resolve().parent

class ConfigManager:    
    def __init__(self, config_path: Optional[str] = None, user_id: Optional[str] = None):
        self.user_id = user_id
        if user_id:
            self.user_dir = PROJECT_ROOT / "users" / user_id
            self.config_path = str(self.user_dir / "configs" / "config.json")
            os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        else:
            self.user_dir = None
            if config_path:
                self.config_path = config_path
            else:
                self.config_path = str(PROJECT_ROOT / "configs" / "config.json")
                
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
            default_conf = self._default_config()
            # If it's a user config, write the default right away for future customization
            if self.user_id:
                self.config = default_conf
                self.save_config()
            return default_conf
    
    def _default_config(self) -> Dict:
        return {
            "auto_email": False,
            "scraper_keys_path": str(PROJECT_ROOT / "configs" / "scraper_keys.json"),
            "gemini_keys_path": str(PROJECT_ROOT / "configs" / "gemini_keys.json"),
            "max_posts_per_key": 250,
            "enable_parallel_extraction": True,
            "log_level": "INFO",
            "candidate_name": "Your Name",
            "candidate_email": "your.email@example.com",
            "resume_path": "",
            "smtp_server": "smtp.gmail.com",
            "smtp_port": 587,
            "sender_email": "",
            "sender_password": ""
        }
    
    def get(self, key: str, default=None):
        return self.config.get(key, default)
    
    def set(self, key: str, value):
        self.config[key] = value
        self.save_config()
    
    def is_auto_email_enabled(self) -> bool:
        return self.config.get("auto_email", False)
    
    def save_config(self):
        """Save current config to file"""
        try:
            os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=2, ensure_ascii=False)
            logging.info(f"Config saved to {self.config_path}")
        except Exception as e:
            logging.error(f"Failed to save config: {e}")
            raise
