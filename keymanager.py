from typing import Dict, List, Optional, Union, Tuple
import json
import os
import logging
from dotenv import load_dotenv
from datetime import datetime   
load_dotenv()


class KeyManager:
    
    def __init__(self, keys_file: str, key_type: str = "scraper"):
        self.keys_file = keys_file
        self.key_type = key_type
        self.keys_data = self._load_keys()
        self.current_key_index = 0
    
    def _load_keys(self) -> Dict:
        """Load keys from JSON file"""
        if not os.path.exists(self.keys_file):
            logging.error(f"Keys file {self.keys_file} not found")
            return {"keys": []}
        
        try:
            with open(self.keys_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if self.key_type == "gemini":
                    # Convert simple list format to structured format for consistency
                    if isinstance(data.get("keys"), list) and data["keys"] and isinstance(data["keys"][0], str):
                        return {"keys": data["keys"]}
                return data
        except Exception as e:
            logging.error(f"Error loading keys from {self.keys_file}: {e}")
            return {"keys": []}
    
    def _save_keys(self):
        """Save updated keys data to JSON file"""
        try:
            with open(self.keys_file, 'w', encoding='utf-8') as f:
                json.dump(self.keys_data, f, indent=2)
        except Exception as e:
            logging.error(f"Error saving keys to {self.keys_file}: {e}")
    
    def get_available_keys(self) -> List[Union[str, Dict]]:
        """Get list of available keys"""
        keys = self.keys_data.get("keys", [])
        
        if self.key_type == "scraper":
            # Filter active keys with remaining quota
            available = []
            for key_info in keys:
                if (key_info.get("active", True) and 
                    key_info.get("current_usage", 0) < key_info.get("quota_limit", 2000)):
                    available.append(key_info)
            return available
        else:  # gemini keys
            return [key for key in keys if key]  # Filter out empty keys
    
    def get_next_key(self) -> Optional[Union[str, Dict]]:
        """Get next available key using round-robin"""
        available_keys = self.get_available_keys()
        
        if not available_keys:
            logging.error(f"No available {self.key_type} keys")
            return None
        
        key = available_keys[self.current_key_index % len(available_keys)]
        self.current_key_index = (self.current_key_index + 1) % len(available_keys)
        
        return key
    
    def update_usage(self, key_identifier: str, usage_count: int):
        """Update usage count for a scraper key"""
        if self.key_type != "scraper":
            return
        
        for key_info in self.keys_data.get("keys", []):
            if key_info.get("key") == key_identifier:
                key_info["current_usage"] = key_info.get("current_usage", 0) + usage_count
                key_info["last_used"] = datetime.now().isoformat()
                break
        
        self._save_keys()
        logging.info(f"Updated usage for key {key_identifier[:10]}...: +{usage_count}")
    
    def reset_daily_usage(self):
        if self.key_type != "scraper":
            return

        for key_info in self.keys_data.get("keys", []):
            key_info["current_usage"] = 0
        
        self._save_keys()
        logging.info("Reset daily usage for all scraper keys")
    
    def get_usage_stats(self) -> Dict:
        if self.key_type != "scraper":
            return {"total_keys": len(self.get_available_keys())}
        
        stats = {
            "total_keys": len(self.keys_data.get("keys", [])),
            "active_keys": 0,
            "total_usage": 0,
            "total_quota": 0,
            "keys_exhausted": 0
        }
        
        for key_info in self.keys_data.get("keys", []):
            if key_info.get("active", True):
                stats["active_keys"] += 1
                usage = key_info.get("current_usage", 0)
                quota = key_info.get("quota_limit", 2000)
                stats["total_usage"] += usage
                stats["total_quota"] += quota
                
                if usage >= quota:
                    stats["keys_exhausted"] += 1
        
        return stats
    
