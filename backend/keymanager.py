from typing import Dict, List, Optional, Union, Tuple
import json
import os
import logging
from dotenv import load_dotenv
from datetime import datetime, timedelta
import time
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
        """Get list of available keys (returns key strings for gemini, key dicts for scrapers)"""
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
            # Handle both old string format and new dict format with quota tracking
            result = []
            for key in keys:
                if isinstance(key, dict):
                    key_str = key.get("key")
                    if key_str:
                        result.append(key_str)
                elif isinstance(key, str) and key:
                    result.append(key)
            return result
    
    def get_next_key(self) -> Optional[Union[str, Dict]]:
        """Get next available key using round-robin"""
        available_keys = self.get_available_keys()

        if not available_keys:
            logging.error(f"No available {self.key_type} keys")
            return None

        if self.key_type == "gemini":
            # For Gemini, filter by quota availability
            quota_ok_keys = [k for k in available_keys if self._is_quota_available(k)]
            if not quota_ok_keys:
                logging.warning(f"All Gemini keys have exhausted quotas")
                return None
            key = quota_ok_keys[self.current_key_index % len(quota_ok_keys)]
        else:
            key = available_keys[self.current_key_index % len(available_keys)]

        self.current_key_index = (self.current_key_index + 1) % len(available_keys)

        return key
    
    def _is_quota_available(self, key_info: Dict) -> bool:
        """Check if a Gemini key has quota available for all limits"""
        if self.key_type != "gemini":
            return True

        now = datetime.now()
        requests_today = key_info.get("requests_today", 0)
        tokens_today = key_info.get("tokens_today", 0)
        requests_per_day_limit = key_info.get("requests_per_day_limit", 500)

        # Reset daily counters if past midnight
        last_request_str = key_info.get("last_request_time")
        if last_request_str:
            try:
                last_request = datetime.fromisoformat(last_request_str.replace('Z', '+00:00'))
                if last_request.date() != now.date():
                    requests_today = 0
                    tokens_today = 0
            except:
                pass

        # Check all quota limits
        if requests_today >= requests_per_day_limit:
            return False

        # Minute-window quotas will be checked in track_request
        return True

    def track_request(self, key_identifier: str, token_count: int = 0) -> bool:
        """Track a request for a Gemini key, return True if successful"""
        if self.key_type != "gemini":
            return True

        for key_info in self.keys_data.get("keys", []):
            if key_info.get("key") == key_identifier or key_info == key_identifier:
                now = datetime.now()
                last_request_str = key_info.get("last_request_time")

                # Reset daily counters if past midnight
                if last_request_str:
                    try:
                        last_request = datetime.fromisoformat(last_request_str.replace('Z', '+00:00'))
                        if last_request.date() != now.date():
                            key_info["requests_today"] = 0
                            key_info["tokens_today"] = 0
                    except:
                        pass

                # Reset minute counters if past 1 minute
                last_minute_str = key_info.get("last_minute_timestamp")
                if last_minute_str:
                    try:
                        last_minute = datetime.fromisoformat(last_minute_str.replace('Z', '+00:00'))
                        if (now - last_minute).total_seconds() >= 60:
                            key_info["requests_this_minute"] = 0
                            key_info["tokens_this_minute"] = 0
                    except:
                        pass

                # Increment counters
                key_info["requests_today"] = key_info.get("requests_today", 0) + 1
                key_info["tokens_today"] = key_info.get("tokens_today", 0) + token_count
                key_info["requests_this_minute"] = key_info.get("requests_this_minute", 0) + 1
                key_info["tokens_this_minute"] = key_info.get("tokens_this_minute", 0) + token_count
                key_info["last_request_time"] = now.isoformat() + "Z"
                key_info["last_minute_timestamp"] = now.isoformat() + "Z"

                # Check if we're approaching limits
                requests_per_day = key_info.get("requests_per_day_limit", 500)
                requests_per_minute = key_info.get("requests_per_minute_limit", 15)
                tokens_per_minute = key_info.get("tokens_per_minute_limit", 250000)

                if key_info["requests_this_minute"] > requests_per_minute:
                    logging.warning(f"Gemini key requests/minute exceeded: {key_info['requests_this_minute']}/{requests_per_minute}")
                    return False
                if key_info["tokens_this_minute"] > tokens_per_minute:
                    logging.warning(f"Gemini key tokens/minute exceeded: {key_info['tokens_this_minute']}/{tokens_per_minute}")
                    return False
                if key_info["requests_today"] > requests_per_day:
                    logging.warning(f"Gemini key requests/day exceeded: {key_info['requests_today']}/{requests_per_day}")
                    return False

                self._save_keys()
                logging.info(f"Tracked request for Gemini key. Reqs/day: {key_info['requests_today']}/{requests_per_day}, Tokens/min: {key_info['tokens_this_minute']}/{tokens_per_minute}")
                return True

        logging.error(f"Gemini key {key_identifier[:10]}... not found")
        return False

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
    
