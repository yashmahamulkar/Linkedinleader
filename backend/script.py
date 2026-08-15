#!/usr/bin/env python3
"""
LinkedIn Lead Generation Runner
Integrates scraping, extraction, and email sending in a complete workflow
"""

import html
import json
import os
import re
import sys
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Tuple, Optional
from pathlib import Path

# Import scraper
from apify_client import ApifyClient
import requests
from dateutil import parser as date_parser
from bs4 import BeautifulSoup
import feedparser
import time as time_module
from urllib.parse import urlencode

from config import ConfigManager
from keymanager import KeyManager
from preferencemanager import PreferenceManager
from emailmanager import TemplateLoader, EmailTemplateGenerator, ColdEmailSystem
from extractor import LinkedInLeadExtractor, ExtractedLead
from datetime import datetime

PROJECT_ROOT = Path(__file__).resolve().parent
# DEFAULT_CONFIG_PATH = Path("/home/Lazycat/mysite/configs/config.json")
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "configs" / "config.json"
# DEFAULT_SCRAPER_KEYS_PATH = Path("/home/Lazycat/mysite/configs/scraper_keys.json")
DEFAULT_SCRAPER_KEYS_PATH = PROJECT_ROOT / "configs" / "scraper_keys.json"

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def strip_html(value: Optional[str]) -> str:
    """Flatten an HTML fragment to readable plain text (block tags become newlines)."""
    if not value or not isinstance(value, str):
        return ""
    text = re.sub(r"(?i)<br\s*/?>", "\n", value)
    text = re.sub(r"(?i)</(p|div|li|ul|ol|h[1-6]|tr)>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "- ", text)
    text = _HTML_TAG_RE.sub("", text)
    text = html.unescape(text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return _BLANK_LINES_RE.sub("\n\n", text).strip()


def _posted_within_hours(posted_value, hours: int = 24) -> bool:
    """True if `posted_value` (ISO string, unix-epoch string/int, or None) parses to a moment
    within the last `hours` hours. Direct-HTTP job-board scrapers (Himalayas/RemoteOK/Remotive/
    Jobicy) have no server-side date filter like Apify's datePosted/f_TPR, so this enforces the
    "24 hrs only" constraint client-side. Unparseable/missing dates are excluded rather than kept,
    since we can't verify they're fresh.
    """
    if posted_value is None or posted_value == "":
        return False
    try:
        if isinstance(posted_value, (int, float)) or (isinstance(posted_value, str) and posted_value.isdigit()):
            posted_dt = datetime.fromtimestamp(int(posted_value), tz=timezone.utc)
        else:
            posted_dt = date_parser.parse(str(posted_value))
            if posted_dt.tzinfo is None:
                posted_dt = posted_dt.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError, OSError, date_parser.ParserError):
        return False
    return (datetime.now(timezone.utc) - posted_dt) <= timedelta(hours=hours)


def _load_scraper_service_keys() -> dict:
    """Read optional per-service API credentials (e.g. {"adzuna": {"app_id": ..., "app_key":
    ...}}, {"usajobs": {"api_key": ..., "email": ...}}) from configs/scraper_keys.json. That
    file's top-level "keys" array (Apify key rotation) is a separate, unrelated concern --
    this only reads sibling keys alongside it, and is a no-op if they're absent.
    """
    try:
        with open(DEFAULT_SCRAPER_KEYS_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return {k: v for k, v in data.items() if k != "keys"}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


class LinkedInScraper:
    """LinkedIn scraper with key management integration"""

    def __init__(self, config_manager: ConfigManager):
        # self.config_manager = ConfigManager('/home/Lazycat/mysite/configs/config.json')
        self.config_manager = config_manager or ConfigManager(str(DEFAULT_CONFIG_PATH))
        # scraper_keys_path = config_manager.get("scraper_keys_path", "/home/Lazycat/mysite/configs/scraper_keys.json")
        scraper_keys_path = self.config_manager.get("scraper_keys_path", str(DEFAULT_SCRAPER_KEYS_PATH))
        self.key_manager = KeyManager(scraper_keys_path, "scraper")
        self.client = None
        print(self.config_manager.config)
        self._initialize_client()

    def _initialize_client(self):
        """Initialize Apify client with available key"""
        key_info = self.key_manager.get_next_key()
        if not key_info:
            raise ValueError("No available scraper API keys")

        api_key = key_info.get("key") if isinstance(key_info, dict) else key_info
        self.client = ApifyClient(api_key)
        self.current_key = key_info
        logging.info(f"Initialized scraper with key: {api_key[:15]}...")

    def _resolve_dataset_id(self, run) -> str:
        """Resolve dataset id across apify-client versions."""
        if isinstance(run, dict):
            dataset_id = run.get("defaultDatasetId")
        else:
            dataset_id = getattr(run, "defaultDatasetId", None)
            if dataset_id is None:
                dataset_id = getattr(run, "default_dataset_id", None)
            if dataset_id is None and hasattr(run, "to_dict"):
                dataset_id = run.to_dict().get("defaultDatasetId")
            if dataset_id is None and hasattr(run, "data"):
                dataset_id = run.data.get("defaultDatasetId")

        if not dataset_id:
            raise ValueError("Apify run did not provide a default dataset id")

        return dataset_id

    def scrape_linkedin_posts(self, search_urls: List[str], limit_per_source: int = 50) -> str:
        """
        Scrape LinkedIn posts and save to JSON file

        Args:
            search_urls: List of LinkedIn search URLs
            limit_per_source: Number of posts to scrape per URL

        Returns:
            Path to the saved JSON file
        """
        if not self.client:
            raise ValueError("Scraper client not initialized")

        # Check quota before scraping
        if isinstance(self.current_key, dict):
            current_usage = self.current_key.get("current_usage", 0)
            quota_limit = self.current_key.get("quota_limit", 2000)
            estimated_usage = len(search_urls) * limit_per_source

            if current_usage + estimated_usage > quota_limit:
                logging.warning(f"Estimated usage ({estimated_usage}) would exceed quota limit")
                # Try to get another key
                self._initialize_client()

        # Prepare the Actor input
        run_input = {
            "urls": search_urls,
            "limitPerSource": limit_per_source,
            "deepScrape": False,
            "rawData": False,
        }

        logging.info(f"Starting scrape with {len(search_urls)} URLs, {limit_per_source} posts per URL")

        try:
            # Run the Actor and wait for it to finish
            run = self.client.actor("Wpp1BZ6yGWjySadk3").call(run_input=run_input)
            dataset_id = self._resolve_dataset_id(run)

            # Fetch Actor results
            results = []
            for item in self.client.dataset(dataset_id).iterate_items():
                results.append(item)

            # Generate filename with timestamp
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"linkedin_data.json"

            # Save results to JSON file
            with open(filename, "w", encoding="utf-8") as f:
                json.dump(results, f, ensure_ascii=False, indent=2)

            # Update usage statistics
            actual_posts = len(results)
            if isinstance(self.current_key, dict):
                self.key_manager.update_usage(self.current_key["key"], actual_posts)

            logging.info(f"Scraped {actual_posts} posts and saved to {filename}")
            return filename

        except Exception as e:
            logging.error(f"Scraping failed: {e}")
            raise

    def scrape_linkedin_jobs(self, job_input: Dict, max_items: Optional[int] = None,
                             output_filename: str = "linkedin_jobs.json") -> str:
        """
        Scrape LinkedIn job search results and save to JSON file.

        Args:
            job_input: Actor input for the LinkedIn jobs scraper
            max_items: Optional limit for max items
            output_filename: Path to write results

        Returns:
            Path to the saved JSON file
        """
        if not self.client:
            raise ValueError("Scraper client not initialized")

        run_input = dict(job_input or {})
        output_limit = None
        if max_items is not None:
            try:
                output_limit = int(max_items)
            except (TypeError, ValueError):
                output_limit = None

        actor_max_items = run_input.get("maxItems")
        if actor_max_items in (None, ""):
            actor_max_items = output_limit if output_limit is not None else 150

        try:
            actor_max_items = int(actor_max_items)
        except (TypeError, ValueError):
            actor_max_items = 150

        if actor_max_items < 150:
            if output_limit and output_limit < 150:
                logging.info("Jobs actor requires maxItems >= 150; scraping 150 and trimming to requested limit")
            else:
                logging.info("Jobs actor requires maxItems >= 150; using 150")
            actor_max_items = 150

        run_input["maxItems"] = actor_max_items

        logging.info("Starting job search scrape")

        try:
            run = self.client.actor("2rJKkhh7vjpX7pvjg").call(run_input=run_input)
            dataset_id = self._resolve_dataset_id(run)

            results = []
            for item in self.client.dataset(dataset_id).iterate_items():
                results.append(item)

            actual_items = len(results)
            if output_limit is not None and output_limit > 0 and actual_items > output_limit:
                results = results[:output_limit]

            with open(output_filename, "w", encoding="utf-8") as f:
                json.dump(results, f, ensure_ascii=False, indent=2)

            saved_items = len(results)
            if isinstance(self.current_key, dict):
                self.key_manager.update_usage(self.current_key["key"], actual_items)

            logging.info(f"Scraped {actual_items} jobs and saved {saved_items} to {output_filename}")
            return output_filename

        except Exception as e:
            logging.error(f"Job scraping failed: {e}")
            raise

    def scrape_indeed_jobs(self, job_input: Dict, max_items: Optional[int] = None,
                            output_filename: str = "indeed_jobs.json") -> str:
        """
        Scrape Indeed job search results (via the Indeed Jobs Scraper actor) and save to JSON file.

        Args:
            job_input: Actor input, e.g. {"country": "in", "title": "...", "location": "...", "limit": 50, "datePosted": "7"}
            max_items: Optional override for the actor's "limit" input
            output_filename: Path to write results

        Returns:
            Path to the saved JSON file
        """
        if not self.client:
            raise ValueError("Scraper client not initialized")

        run_input = dict(job_input or {})
        if max_items is not None:
            try:
                run_input["limit"] = int(max_items)
            except (TypeError, ValueError):
                pass

        logging.info("Starting Indeed job search scrape")

        try:
            run = self.client.actor("TrtlecxAsNRbKl1na").call(run_input=run_input)
            dataset_id = self._resolve_dataset_id(run)

            results = []
            for item in self.client.dataset(dataset_id).iterate_items():
                results.append(item)

            with open(output_filename, "w", encoding="utf-8") as f:
                json.dump(results, f, ensure_ascii=False, indent=2)

            actual_items = len(results)
            if isinstance(self.current_key, dict):
                self.key_manager.update_usage(self.current_key["key"], actual_items)

            logging.info(f"Scraped {actual_items} Indeed jobs and saved to {output_filename}")
            return output_filename

        except Exception as e:
            logging.error(f"Indeed scraping failed: {e}")
            raise

    def scrape_glassdoor_jobs(self, job_input: Dict, max_items: Optional[int] = None,
                              output_filename: str = "glassdoor_jobs.json") -> str:
        """
        Scrape Glassdoor job search results (via the Glassdoor Jobs Scraper actor) and save to JSON file.

        Args:
            job_input: Actor input, e.g. {"keywords": "...", "location": "...", "daysOld": 30, "limit": 50}
            max_items: Optional override for the actor's "limit" input
            output_filename: Path to write results

        Returns:
            Path to the saved JSON file
        """
        if not self.client:
            raise ValueError("Scraper client not initialized")

        run_input = dict(job_input or {})
        if max_items is not None:
            try:
                run_input["limit"] = int(max_items)
            except (TypeError, ValueError):
                pass

        logging.info("Starting Glassdoor job search scrape")

        try:
            run = self.client.actor("5OaooRg0FxlRF0L1B").call(run_input=run_input)
            dataset_id = self._resolve_dataset_id(run)

            results = []
            for item in self.client.dataset(dataset_id).iterate_items():
                results.append(item)

            with open(output_filename, "w", encoding="utf-8") as f:
                json.dump(results, f, ensure_ascii=False, indent=2)

            actual_items = len(results)
            if isinstance(self.current_key, dict):
                self.key_manager.update_usage(self.current_key["key"], actual_items)

            logging.info(f"Scraped {actual_items} Glassdoor jobs and saved to {output_filename}")
            return output_filename

        except Exception as e:
            logging.error(f"Glassdoor scraping failed: {e}")
            raise

class BaseScraper:
    def scrape(self, runner, search_params: Dict) -> List[Dict]:
        """Scrape data and return a list of raw items"""
        raise NotImplementedError

class ApifyPostsScraper(BaseScraper):
    def scrape(self, runner, search_params: Dict) -> List[Dict]:
        search_urls = search_params.get("search_urls") or create_default_search_urls()
        limit = search_params.get("limit") or 50
        if not runner.scraper:
            runner._initialize_scraper()
        filename = runner.scraper.scrape_linkedin_posts(search_urls, limit)
        with open(filename, 'r', encoding='utf-8') as f:
            return json.load(f)

class ApifyJobsScraper(BaseScraper):
    def scrape(self, runner, search_params: Dict) -> List[Dict]:
        job_input = search_params.get("job_input") or create_default_job_search_input()
        limit = search_params.get("limit") or 50
        if not runner.scraper:
            runner._initialize_scraper()
        filename = runner.scraper.scrape_linkedin_jobs(job_input, max_items=limit)
        with open(filename, 'r', encoding='utf-8') as f:
            # Return raw actor items -- normalization happens exactly once, downstream in
            # LinkedInRunner._normalize_job_items() at extraction time. Normalizing here too
            # would double-normalize items pulled back out of the DB later (urn/company/
            # location/description all get lost on a second pass over already-normalized data).
            return json.load(f)

class ApifyIndeedJobsScraper(BaseScraper):
    def scrape(self, runner, search_params: Dict) -> List[Dict]:
        indeed_input = search_params.get("indeed_input") or create_default_indeed_search_input()
        limit = search_params.get("limit") or 50
        if not runner.scraper:
            runner._initialize_scraper()
        filename = runner.scraper.scrape_indeed_jobs(indeed_input, max_items=limit)
        with open(filename, 'r', encoding='utf-8') as f:
            return json.load(f)  # raw actor items -- normalized once at extraction time

class ApifyGlassdoorJobsScraper(BaseScraper):
    def scrape(self, runner, search_params: Dict) -> List[Dict]:
        glassdoor_input = search_params.get("glassdoor_input") or create_default_glassdoor_search_input()
        limit = search_params.get("limit") or 50
        if not runner.scraper:
            runner._initialize_scraper()
        filename = runner.scraper.scrape_glassdoor_jobs(glassdoor_input, max_items=limit)
        with open(filename, 'r', encoding='utf-8') as f:
            return json.load(f)  # raw actor items -- normalized once at extraction time

DEFAULT_REMOTE_SEARCH_TERMS = ["software engineer", "developer", "intern", "backend", "frontend"]


class HimalayasScraper(BaseScraper):
    """Direct-HTTP scraper for himalayas.app's public remote-jobs API. No auth needed.
    Ported from CareerPulse's app/scrapers/himalayas.py (httpx/async) to requests/sync."""

    API_URL = "https://himalayas.app/jobs/api"
    PAGE_SIZE = 50
    MAX_PAGES = 4  # capped -- we only keep last-24h postings anyway, no need to page deep

    def scrape(self, runner, search_params: Dict) -> List[Dict]:
        terms = [t.lower() for t in (search_params.get("keywords") or DEFAULT_REMOTE_SEARCH_TERMS)]
        items = []
        for page in range(self.MAX_PAGES):
            try:
                resp = requests.get(self.API_URL, params={"limit": self.PAGE_SIZE, "offset": page * self.PAGE_SIZE}, timeout=20)
                resp.raise_for_status()
                data = resp.json()
            except (requests.RequestException, ValueError) as e:
                logging.error(f"Himalayas scrape failed on page {page}: {e}")
                break
            listings = data.get("jobs", [])
            if not listings:
                break
            for item in listings:
                if not _posted_within_hours(item.get("pubDate")):
                    continue
                searchable = f"{item.get('title', '')} {item.get('description', '')}".lower()
                if terms and not any(term in searchable for term in terms):
                    continue
                items.append(item)
        return items


class RemoteOKScraper(BaseScraper):
    """Direct-HTTP scraper for remoteok.com's public JSON feed. No auth needed.
    Ported from CareerPulse's app/scrapers/remoteok.py (httpx/async) to requests/sync."""

    API_URL = "https://remoteok.com/api"

    def scrape(self, runner, search_params: Dict) -> List[Dict]:
        terms = [t.lower() for t in (search_params.get("keywords") or DEFAULT_REMOTE_SEARCH_TERMS)]
        try:
            resp = requests.get(self.API_URL, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as e:
            logging.error(f"RemoteOK scrape failed: {e}")
            return []

        # First element is a metadata/legal notice object, not a job
        listings = data[1:] if isinstance(data, list) and len(data) > 1 else []
        items = []
        for item in listings:
            if not _posted_within_hours(item.get("date")):
                continue
            searchable = f"{item.get('position', '')} {item.get('description', '')} {' '.join(item.get('tags') or [])}".lower()
            if terms and not any(term in searchable for term in terms):
                continue
            items.append(item)
        return items


class RemotiveScraper(BaseScraper):
    """Direct-HTTP scraper for remotive.com's public jobs API. No auth needed.
    Ported from CareerPulse's app/scrapers/remotive.py (httpx/async) to requests/sync."""

    API_URL = "https://remotive.com/api/remote-jobs"
    CATEGORIES = ["software-dev", "devops", "data"]

    def scrape(self, runner, search_params: Dict) -> List[Dict]:
        terms = [t.lower() for t in (search_params.get("keywords") or DEFAULT_REMOTE_SEARCH_TERMS)]
        items = []
        for category in self.CATEGORIES:
            try:
                resp = requests.get(self.API_URL, params={"category": category, "limit": 50}, timeout=20)
                resp.raise_for_status()
                data = resp.json()
            except (requests.RequestException, ValueError) as e:
                logging.error(f"Remotive scrape failed for category '{category}': {e}")
                continue
            for item in data.get("jobs", []):
                if not _posted_within_hours(item.get("publication_date")):
                    continue
                searchable = f"{item.get('title', '')} {item.get('description', '')}".lower()
                if terms and not any(term in searchable for term in terms):
                    continue
                items.append(item)
        return items


class JobicyScraper(BaseScraper):
    """Direct-HTTP scraper for jobicy.com's public remote-jobs API. No auth needed.
    Ported from CareerPulse's app/scrapers/jobicy.py (httpx/async) to requests/sync."""

    API_URL = "https://jobicy.com/api/v2/remote-jobs"
    DEFAULT_TAGS = ["engineer", "backend", "data"]

    def scrape(self, runner, search_params: Dict) -> List[Dict]:
        tags = search_params.get("tags") or self.DEFAULT_TAGS
        terms = [t.lower() for t in (search_params.get("keywords") or DEFAULT_REMOTE_SEARCH_TERMS)]
        seen_ids = set()
        items = []
        for tag in tags:
            try:
                resp = requests.get(self.API_URL, params={"count": 50, "tag": tag}, timeout=20)
                resp.raise_for_status()
                data = resp.json()
            except (requests.RequestException, ValueError) as e:
                logging.error(f"Jobicy scrape failed for tag '{tag}': {e}")
                continue
            for item in data.get("jobs", []):
                job_id = item.get("id")
                if job_id is not None and job_id in seen_ids:
                    continue
                seen_ids.add(job_id)
                if not _posted_within_hours(item.get("pubDate")):
                    continue
                searchable = f"{item.get('jobTitle', '')} {item.get('jobDescription', '')}".lower()
                if terms and not any(term in searchable for term in terms):
                    continue
                items.append(item)
        return items


class ArbeitnowScraper(BaseScraper):
    """Direct-HTTP scraper for arbeitnow.com's public job-board API. No auth needed.
    Ported from CareerPulse's app/scrapers/arbeitnow.py (httpx/async) to requests/sync."""

    API_URL = "https://www.arbeitnow.com/api/job-board-api"
    MAX_PAGES = 3

    def scrape(self, runner, search_params: Dict) -> List[Dict]:
        terms = [t.lower() for t in (search_params.get("keywords") or DEFAULT_REMOTE_SEARCH_TERMS)]
        items = []
        for page in range(1, self.MAX_PAGES + 1):
            try:
                resp = requests.get(self.API_URL, params={"page": page}, timeout=20)
                resp.raise_for_status()
                data = resp.json()
            except (requests.RequestException, ValueError) as e:
                logging.error(f"Arbeitnow scrape failed on page {page}: {e}")
                break
            listings = data.get("data", [])
            if not listings:
                break
            for item in listings:
                # created_at is a unix epoch -- present on every item, unlike the upstream
                # CareerPulse port which discarded it and could never honor a 24h window.
                if not _posted_within_hours(item.get("created_at")):
                    continue
                searchable = f"{item.get('title', '')} {item.get('description', '')} {' '.join(item.get('tags') or [])}".lower()
                if terms and not any(term in searchable for term in terms):
                    continue
                items.append(item)
        return items


class HackerNewsScraper(BaseScraper):
    """Direct-HTTP scraper for Hacker News 'who is hiring' threads (Algolia search + Firebase
    item API). No auth needed. Ported from CareerPulse's app/scrapers/hackernews.py
    (httpx/async, asyncio.gather comment fan-out) to requests/sync, sequential."""

    ALGOLIA_SEARCH_URL = "https://hn.algolia.com/api/v1/search"
    HN_ITEM_URL = "https://hacker-news.firebaseio.com/v0/item/{id}.json"
    MAX_COMMENTS = 150

    def scrape(self, runner, search_params: Dict) -> List[Dict]:
        terms = [t.lower() for t in (search_params.get("keywords") or DEFAULT_REMOTE_SEARCH_TERMS)]
        one_month_ago = int(time_module.time()) - 60 * 60 * 24 * 35
        try:
            resp = requests.get(
                self.ALGOLIA_SEARCH_URL,
                params={"query": "who is hiring", "tags": "story,ask_hn", "numericFilters": f"created_at_i>{one_month_ago}"},
                timeout=20,
            )
            resp.raise_for_status()
            hits = resp.json().get("hits", [])
        except (requests.RequestException, ValueError) as e:
            logging.error(f"HackerNews search failed: {e}")
            return []
        if not hits:
            return []

        thread_id = hits[0]["objectID"]
        try:
            resp = requests.get(self.HN_ITEM_URL.format(id=thread_id), timeout=20)
            resp.raise_for_status()
            kids = resp.json().get("kids", [])[:self.MAX_COMMENTS]
        except (requests.RequestException, ValueError) as e:
            logging.error(f"HackerNews thread fetch failed: {e}")
            return []

        items = []
        for kid_id in kids:
            try:
                resp = requests.get(self.HN_ITEM_URL.format(id=kid_id), timeout=20)
                resp.raise_for_status()
                comment = resp.json()
            except (requests.RequestException, ValueError):
                continue
            if not comment or comment.get("deleted") or not comment.get("text"):
                continue
            # Firebase's item API does carry a per-comment unix-epoch `time` field -- the
            # upstream CareerPulse port never read it and so could never honor a 24h window.
            if not _posted_within_hours(comment.get("time")):
                continue
            plain_text = BeautifulSoup(comment["text"], "html.parser").get_text(separator="\n")
            lines = [l.strip() for l in plain_text.split("\n") if l.strip()]
            if not lines:
                continue
            searchable = plain_text.lower()
            if terms and not any(term in searchable for term in terms):
                continue
            comment["_hn_plain_text"] = plain_text
            comment["_hn_first_line"] = lines[0]
            items.append(comment)
        return items


class WeWorkRemotelyScraper(BaseScraper):
    """Direct-HTTP scraper for weworkremotely.com's public RSS feeds. No auth needed.
    Ported from CareerPulse's app/scrapers/weworkremotely.py (httpx/async) to requests/sync."""

    FEED_URLS = [
        "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss",
        "https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss",
        "https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss",
    ]

    def scrape(self, runner, search_params: Dict) -> List[Dict]:
        terms = [t.lower() for t in (search_params.get("keywords") or [])]
        items = []
        for feed_url in self.FEED_URLS:
            try:
                resp = requests.get(feed_url, timeout=20)
                resp.raise_for_status()
            except requests.RequestException as e:
                logging.error(f"WeWorkRemotely scrape failed for {feed_url}: {e}")
                continue
            feed = feedparser.parse(resp.content)
            for entry in feed.entries:
                if not _posted_within_hours(entry.get("published")):
                    continue
                title = entry.get("title", "")
                searchable = f"{title} {entry.get('summary', '')}".lower()
                if terms and not any(term in searchable for term in terms):
                    continue
                items.append({
                    "title": title,
                    "summary": entry.get("summary", ""),
                    "link": entry.get("link", ""),
                    "published": entry.get("published"),
                })
        return items


class GreenhouseScraper(BaseScraper):
    """Direct-HTTP scraper for the Greenhouse job-board API. No auth needed, but unlike every
    other source here it's scoped to whatever companies the user explicitly targets (Greenhouse
    has no cross-company search -- each company's postings live at their own board token, e.g.
    boards.greenhouse.io/<slug>). Returns nothing if no target companies are configured.
    Ported from CareerPulse's app/scrapers/greenhouse.py (httpx/async) to requests/sync."""

    API_BASE = "https://boards-api.greenhouse.io/v1/boards"

    def scrape(self, runner, search_params: Dict) -> List[Dict]:
        companies = search_params.get("target_companies") or []
        if not companies:
            logging.info("Greenhouse: no target companies configured, skipping")
            return []

        terms = [t.lower() for t in (search_params.get("keywords") or [])]
        items = []
        for company in companies:
            try:
                resp = requests.get(f"{self.API_BASE}/{company}/jobs", params={"content": "true"}, timeout=20)
                if resp.status_code == 404:
                    logging.warning(f"Greenhouse: company '{company}' not found (404) -- check the board slug")
                    continue
                resp.raise_for_status()
                data = resp.json()
            except (requests.RequestException, ValueError) as e:
                logging.error(f"Greenhouse scrape failed for '{company}': {e}")
                continue

            for job in data.get("jobs", []):
                # Greenhouse only exposes a date, not a time -- so freshness comparisons against
                # "now" are date-granularity, not hour-granularity, but still enforce 24h.
                if not _posted_within_hours(job.get("updated_at")):
                    continue
                searchable = f"{job.get('title', '')} {job.get('content', '')}".lower()
                if terms and not any(term in searchable for term in terms):
                    continue
                job["_target_company_slug"] = company
                items.append(job)
        return items


class BuiltInScraper(BaseScraper):
    """Direct-HTTP scraper for builtin.com's remote job listings (JSON-LD embedded in listing
    and detail pages). No auth needed. Ported from CareerPulse's app/scrapers/builtin.py
    (httpx/async, bs4) to requests/sync.
    """

    BASE_URL = "https://builtin.com"
    LISTING_PATH = "/jobs/remote/dev-engineering"
    MAX_DETAIL_FETCHES = 25

    def _parse_listing_jsonld(self, html_text: str) -> List[Dict]:
        soup = BeautifulSoup(html_text, "html.parser")
        stubs = []
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string)
            except (json.JSONDecodeError, TypeError):
                continue
            graph = data.get("@graph", [data]) if isinstance(data, dict) else []
            for node in graph:
                if node.get("@type") == "ItemList":
                    for item in node.get("itemListElement", []):
                        if item.get("url") and item.get("name"):
                            stubs.append({"title": item["name"], "url": item["url"], "description": item.get("description", "")})
        return stubs

    def _parse_detail_jsonld(self, html_text: str) -> Optional[Dict]:
        """BuiltIn wraps JobPosting inside a top-level @graph array (same shape as the listing
        page's ItemList), it is never the script's own top-level @type."""
        soup = BeautifulSoup(html_text, "html.parser")
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string)
            except (json.JSONDecodeError, TypeError):
                continue
            graph = data.get("@graph", [data]) if isinstance(data, dict) else []
            for node in graph:
                if node.get("@type") == "JobPosting":
                    return node
        return None

    def scrape(self, runner, search_params: Dict) -> List[Dict]:
        terms = [t.lower() for t in (search_params.get("keywords") or [])]
        try:
            resp = requests.get(f"{self.BASE_URL}{self.LISTING_PATH}", timeout=20)
            resp.raise_for_status()
        except requests.RequestException as e:
            logging.error(f"BuiltIn listing fetch failed: {e}")
            return []

        stubs = self._parse_listing_jsonld(resp.text)
        items = []
        seen_urls = set()
        for stub in stubs[:self.MAX_DETAIL_FETCHES]:
            detail_url = stub["url"]
            if detail_url in seen_urls:
                continue
            seen_urls.add(detail_url)
            try:
                resp = requests.get(detail_url, timeout=20)
                resp.raise_for_status()
            except requests.RequestException:
                continue
            detail = self._parse_detail_jsonld(resp.text)
            if not detail:
                continue
            if not _posted_within_hours(detail.get("datePosted")):
                continue
            searchable = f"{detail.get('title', '')} {detail.get('description', '')}".lower()
            if terms and not any(term in searchable for term in terms):
                continue
            detail["_detail_url"] = detail_url
            items.append(detail)
        return items


class WellfoundScraper(BaseScraper):
    """Direct-HTTP scraper for wellfound.com (formerly AngelList Talent). No auth needed, but
    Wellfound actively blocks automated access -- 403/429 responses are logged and treated as
    an empty result for that role path rather than a hard failure. Ported from CareerPulse's
    app/scrapers/wellfound.py (httpx/async, bs4/__NEXT_DATA__/Apollo/JSON-LD triple-fallback)
    to requests/sync.
    """

    BASE_URL = "https://wellfound.com"
    DEFAULT_ROLE_PATHS = ["/role/r/software-engineer", "/role/r/backend-engineer", "/role/r/full-stack-engineer"]
    BROWSER_HEADERS = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://wellfound.com/",
    }

    def _extract_jobs_from_next_data(self, data: Dict) -> List[Dict]:
        props = data.get("props", {}).get("pageProps", {})
        for key in ["jobs", "jobListings", "listings", "results", "data"]:
            items = props.get(key)
            if isinstance(items, list):
                return items
        return []

    def _parse_jobs(self, html_text: str) -> List[Dict]:
        soup = BeautifulSoup(html_text, "html.parser")
        script = soup.find("script", id="__NEXT_DATA__")
        if script and script.string:
            try:
                return self._extract_jobs_from_next_data(json.loads(script.string))
            except json.JSONDecodeError:
                pass

        jobs = []
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(data, list):
                jobs.extend(item for item in data if item.get("@type") == "JobPosting")
            elif isinstance(data, dict) and data.get("@type") == "JobPosting":
                jobs.append(data)
        return jobs

    def scrape(self, runner, search_params: Dict) -> List[Dict]:
        terms = [t.lower() for t in (search_params.get("keywords") or [])]
        items = []
        seen_urls = set()
        for path in self.DEFAULT_ROLE_PATHS:
            try:
                resp = requests.get(f"{self.BASE_URL}{path}", headers=self.BROWSER_HEADERS, timeout=20)
                if resp.status_code in (403, 429):
                    logging.warning(f"Wellfound blocked ({resp.status_code}) for {path}")
                    continue
                resp.raise_for_status()
            except requests.RequestException as e:
                logging.error(f"Wellfound fetch failed for {path}: {e}")
                continue

            for job in self._parse_jobs(resp.text):
                url = job.get("url") or ""
                if not job.get("title") or (url and url in seen_urls):
                    continue
                if url:
                    seen_urls.add(url)
                if not _posted_within_hours(job.get("datePosted") or job.get("postedAt")):
                    continue
                searchable = f"{job.get('title', '')} {job.get('description', '')}".lower()
                if terms and not any(term in searchable for term in terms):
                    continue
                items.append(job)
        return items


class DiceScraper(BaseScraper):
    """Direct-HTTP scraper for dice.com search results (job data embedded in Next.js streaming
    chunks). No auth needed. Ported from CareerPulse's app/scrapers/dice.py (httpx/async,
    semaphore-bounded detail fetches, 410-handling) to requests/sync, sequential.
    """

    BASE_URL = "https://www.dice.com/jobs"
    DEFAULT_QUERIES = ["software engineer remote", "backend engineer remote"]
    _CHUNK_RE = re.compile(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', re.DOTALL)

    def _extract_jobs_from_html(self, html_text: str) -> List[Dict]:
        chunks = self._CHUNK_RE.findall(html_text)
        if not chunks:
            return []
        try:
            combined = "".join(chunks).encode().decode("unicode_escape")
            idx = combined.find('"jobList":{"data":[')
            if idx < 0:
                return []
            arr_start = combined.find("[", idx)
            arr_end = combined.find('],"meta"', arr_start)
            if arr_end < 0:
                arr_end = combined.find("]}", arr_start)
            if arr_end < 0:
                return []
            return json.loads(combined[arr_start:arr_end + 1])
        except Exception as e:
            logging.debug(f"Dice JSON extraction failed: {e}")
            return []

    def scrape(self, runner, search_params: Dict) -> List[Dict]:
        queries = search_params.get("keywords") or self.DEFAULT_QUERIES
        terms = [t.lower() for t in queries]
        seen_ids = set()
        items = []
        for query in queries:
            params = {"q": query, "countryCode": "US", "radius": "30", "radiusUnit": "mi", "page": "1", "pageSize": "20", "language": "en"}
            try:
                resp = requests.get(f"{self.BASE_URL}?{urlencode(params)}", timeout=20)
                resp.raise_for_status()
            except requests.RequestException as e:
                logging.error(f"Dice fetch failed for '{query}': {e}")
                continue

            for job in self._extract_jobs_from_html(resp.text):
                job_id = job.get("id") or job.get("guid", "")
                if job_id and job_id in seen_ids:
                    continue
                seen_ids.add(job_id)
                if not job.get("title"):
                    continue
                if not _posted_within_hours(job.get("postedDate")):
                    continue
                searchable = f"{job.get('title', '')} {job.get('summary', '')}".lower()
                if terms and not any(term in searchable for term in terms):
                    continue
                items.append(job)
        return items


class AdzunaScraper(BaseScraper):
    """Direct-HTTP scraper for the Adzuna jobs API. Requires an Adzuna app_id/app_key (free
    developer registration) -- read from configs/scraper_keys.json's "adzuna" key, same
    pattern as this project's Gemini/Apify key files. Skips (returns []) if not configured.
    Ported from CareerPulse's app/scrapers/adzuna.py (httpx/async) to requests/sync."""

    API_URL = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"

    def scrape(self, runner, search_params: Dict) -> List[Dict]:
        keys = _load_scraper_service_keys().get("adzuna", {})
        app_id, app_key = keys.get("app_id"), keys.get("app_key")
        if not app_id or not app_key:
            logging.info("Adzuna: no app_id/app_key configured in configs/scraper_keys.json, skipping")
            return []

        country = search_params.get("country") or "in"
        terms = search_params.get("keywords") or DEFAULT_REMOTE_SEARCH_TERMS
        items = []
        seen_urls = set()
        for term in terms:
            params = {"app_id": app_id, "app_key": app_key, "what": term, "results_per_page": 50, "content-type": "application/json"}
            try:
                resp = requests.get(self.API_URL.format(country=country, page=1), params=params, timeout=20)
                resp.raise_for_status()
                data = resp.json()
            except (requests.RequestException, ValueError) as e:
                logging.error(f"Adzuna scrape failed for '{term}': {e}")
                continue
            for item in data.get("results", []):
                job_url = item.get("redirect_url", "")
                if not job_url or job_url in seen_urls:
                    continue
                seen_urls.add(job_url)
                if not _posted_within_hours(item.get("created")):
                    continue
                items.append(item)
        return items


class USAJobsScraper(BaseScraper):
    """Direct-HTTP scraper for the USAJOBS.gov federal jobs API. Requires a free api_key +
    registered email -- read from configs/scraper_keys.json's "usajobs" key. Skips (returns [])
    if not configured. Ported from CareerPulse's app/scrapers/usajobs.py (httpx/async) to
    requests/sync."""

    API_URL = "https://data.usajobs.gov/api/search"

    def scrape(self, runner, search_params: Dict) -> List[Dict]:
        keys = _load_scraper_service_keys().get("usajobs", {})
        api_key, email = keys.get("api_key"), keys.get("email")
        if not api_key:
            logging.info("USAJobs: no api_key configured in configs/scraper_keys.json, skipping")
            return []

        headers = {"Authorization-Key": api_key, "User-Agent": email or "", "Host": "data.usajobs.gov"}
        keywords = search_params.get("keywords") or ["information technology"]
        items = []
        seen_urls = set()
        for keyword in keywords:
            params = {"Keyword": keyword, "JobCategoryCode": "2210", "RemoteIndicator": "True", "ResultsPerPage": 50}
            try:
                resp = requests.get(self.API_URL, headers=headers, params=params, timeout=20)
                resp.raise_for_status()
                data = resp.json()
            except (requests.RequestException, ValueError) as e:
                logging.error(f"USAJobs scrape failed for '{keyword}': {e}")
                continue
            for item in data.get("SearchResult", {}).get("SearchResultItems", []):
                match = item.get("MatchedObjectDescriptor", {})
                url = match.get("PositionURI", "")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                if not _posted_within_hours(match.get("PublicationStartDate")):
                    continue
                items.append(match)
        return items


class ScraperRegistry:
    def __init__(self):
        self._scrapers = {}
        # Register default scrapers
        self.register("posts", ApifyPostsScraper())
        self.register("jobs", ApifyJobsScraper())
        self.register("indeed", ApifyIndeedJobsScraper())
        self.register("glassdoor", ApifyGlassdoorJobsScraper())
        self.register("himalayas", HimalayasScraper())
        self.register("remoteok", RemoteOKScraper())
        self.register("remotive", RemotiveScraper())
        self.register("jobicy", JobicyScraper())
        self.register("arbeitnow", ArbeitnowScraper())
        self.register("hackernews", HackerNewsScraper())
        self.register("weworkremotely", WeWorkRemotelyScraper())
        self.register("greenhouse", GreenhouseScraper())
        # self.register("builtin", BuiltInScraper())  # US-based: builtin.com lists US metro tech jobs only
        self.register("wellfound", WellfoundScraper())
        # self.register("dice", DiceScraper())  # US-based: hardcodes countryCode "US" in query
        self.register("adzuna", AdzunaScraper())
        # self.register("usajobs", USAJobsScraper())  # US-based: USAJOBS.gov, US federal jobs only

    def register(self, name: str, scraper: BaseScraper):
        self._scrapers[name] = scraper

    def get_registered_names(self) -> List[str]:
        return list(self._scrapers.keys())

    def scrape_all(self, runner, search_params: Dict, enabled_scrapers: List[str] = None) -> List[Dict]:
        all_results = []
        target_scrapers = enabled_scrapers or list(self._scrapers.keys())
        for name in target_scrapers:
            if name in self._scrapers:
                try:
                    logging.info(f"Running scraper: {name}")
                    results = self._scrapers[name].scrape(runner, search_params)
                    for item in results:
                        item['_scraper_source'] = name
                    all_results.extend(results)
                    logging.info(f"Scraper '{name}' completed successfully. Got {len(results)} items.")
                except Exception as e:
                    logging.error(f"Error executing scraper '{name}': {e}")
        return all_results

# Global registry instance
scraper_registry = ScraperRegistry()

class LinkedInRunner:
    """Main runner class that orchestrates the complete workflow"""

    def __init__(self, config_path: Optional[str] = None, user_id: Optional[str] = None):
        self.user_id = user_id
        self.config_manager = ConfigManager(config_path, user_id=user_id)
        self.preference_manager = PreferenceManager(user_id=user_id)
        self.template_loader = TemplateLoader(user_id=user_id)

        # Initialize components
        self.scraper = None
        self.extractor = None
        self.email_generator = None
        self.cold_email_system = None

        self._setup_logging()

    def _setup_logging(self):
        """Setup logging based on configuration"""
        log_level = self.config_manager.get("log_level", "INFO")
        logging.getLogger().setLevel(getattr(logging, log_level.upper()))

    def _initialize_scraper(self):
        """Initialize the LinkedIn scraper"""
        try:
            self.scraper = LinkedInScraper(self.config_manager)
            logging.info("✅ Scraper initialized successfully")
        except Exception as e:
            logging.error(f"❌ Failed to initialize scraper: {e}")
            raise

    def _initialize_extractor(self):
        """Initialize the LinkedIn lead extractor"""
        try:
            gemini_keys_path = self.config_manager.get("gemini_keys_path")
            self.extractor = LinkedInLeadExtractor(
                gemini_keys_path=gemini_keys_path,
                strict_filtering=True,
                config_manager=self.config_manager,
                preference_manager=self.preference_manager,
                user_id=self.user_id
            )
            logging.info("✅ Extractor initialized successfully")
        except Exception as e:
            logging.error(f"❌ Failed to initialize extractor: {e}")
            raise

    def _initialize_email_system(self):
        """Initialize the email generation and sending system"""
        try:
            self.email_generator = EmailTemplateGenerator(
                candidate_name=self.config_manager.get("candidate_name", "John Doe"),
                candidate_email=self.config_manager.get("candidate_email", "john.doe@email.com"),
                resume_path=self.config_manager.get("resume_path", ""),
                template_loader=self.template_loader,
                preference_manager=self.preference_manager,
                user_id=self.user_id
            )

            self.cold_email_system = ColdEmailSystem(
                self.extractor,
                self.email_generator,
                self.config_manager,
                user_id=self.user_id
            )
            logging.info("✅ Email system initialized successfully")
        except Exception as e:
            logging.error(f"❌ Failed to initialize email system: {e}")
            raise

    def _build_job_text(self, job_data: Dict) -> str:
        """Build a text blob from job fields for the extractor."""
        parts = []
        title = job_data.get("title") or job_data.get("jobTitle")
        company = job_data.get("companyName") or job_data.get("company")
        location = job_data.get("location") or job_data.get("jobLocation")
        description = (
            job_data.get("description") or
            job_data.get("descriptionText") or
            job_data.get("descriptionHtml") or
            job_data.get("jobDescription")
        )
        contract_type = job_data.get("contractType")
        experience_level = job_data.get("experienceLevel")
        salary_info = job_data.get("salaryInfo")
        apply_url = job_data.get("applyUrl") or job_data.get("jobUrl")

        if title:
            parts.append(f"Title: {title}")
        if company:
            parts.append(f"Company: {company}")
        if location:
            parts.append(f"Location: {location}")
        if contract_type:
            parts.append(f"Contract Type: {contract_type}")
        if experience_level:
            parts.append(f"Experience Level: {experience_level}")
        if salary_info:
            parts.append(f"Salary: {', '.join(salary_info) if isinstance(salary_info, list) else salary_info}")
        if description:
            parts.append(f"Description: {description[:4000]}")
        if apply_url:
            parts.append(f"Apply here: {apply_url}")

        return "\n".join(parts)

    def _normalize_job_items(self, job_items: List[Dict]) -> List[Dict]:
        """Normalize job search items into post-like structures for extraction."""
        normalized = []
        for idx, job in enumerate(job_items):
            urn = (
                job.get("jobId") or
                job.get("jobPostingId") or
                job.get("id") or
                job.get("jobUrl") or
                f"job_{idx}"
            )

            normalized.append({
                "urn": str(urn),
                "text": self._build_job_text(job),
                "authorName": job.get("companyName") or job.get("company") or "",
                "authorHeadline": job.get("location") or "",
                "authorProfileUrl": job.get("companyUrl") or job.get("companyLink") or "",
                "postedAtISO": job.get("postedAt") or job.get("postedAtISO") or job.get("listedAt") or "",
                "url": job.get("jobUrl") or job.get("url") or job.get("applyUrl") or "",
                "title": job.get("title") or job.get("jobTitle") or "",
                "companyLogo": job.get("companyLogo") or "",
                "_raw_item_id": job.get("_raw_item_id"),
                "_source": "jobs",
            })

        return normalized

    def _build_indeed_text(self, job_data: Dict) -> str:
        """Build a text blob from a raw Indeed actor item for the extractor."""
        parts = []
        title = job_data.get("title")
        employer = job_data.get("employer") or {}
        company = employer.get("name")

        location_data = job_data.get("location") or {}
        location_parts = [
            p for p in [
                location_data.get("city"),
                location_data.get("admin3Code"),
                location_data.get("admin1Code"),
                location_data.get("countryName"),
            ] if p
        ]
        location = ", ".join(dict.fromkeys(location_parts))  # dedupe, preserve order

        job_types = job_data.get("jobTypes") or {}
        job_type_str = ", ".join(job_types.values()) if isinstance(job_types, dict) else ""

        salary = job_data.get("baseSalary") or {}
        salary_str = None
        if salary.get("min") or salary.get("max"):
            currency = salary.get("currencyCode") or ""
            unit = salary.get("unitOfWork") or ""
            salary_str = f"{currency} {salary.get('min') or ''}-{salary.get('max') or ''} per {unit}".strip()

        description_data = job_data.get("description")
        description = description_data.get("text") if isinstance(description_data, dict) else description_data
        apply_url = job_data.get("jobUrl") or job_data.get("url")

        if title:
            parts.append(f"Title: {title}")
        if company:
            parts.append(f"Company: {company}")
        if location:
            parts.append(f"Location: {location}")
        if job_type_str:
            parts.append(f"Job Type: {job_type_str}")
        if salary_str:
            parts.append(f"Salary: {salary_str}")
        if description:
            parts.append(f"Description: {description[:4000]}")
        if apply_url:
            parts.append(f"Apply here: {apply_url}")

        return "\n".join(parts)

    def _normalize_indeed_items(self, indeed_items: List[Dict]) -> List[Dict]:
        """Normalize raw Indeed actor items into post-like structures for extraction."""
        normalized = []
        for idx, job in enumerate(indeed_items):
            urn = job.get("key") or job.get("refNum") or f"indeed_{idx}"
            employer = job.get("employer") or {}
            location_data = job.get("location") or {}

            normalized.append({
                # No source prefix -- matches how posts/LinkedIn-jobs use their raw ID directly.
                # raw_items.urn (set by db.insert_raw_items) is this same unprefixed "key" value,
                # so get_raw_item_by_urn() lookups (post-content enrichment) actually match.
                "urn": str(urn),
                "text": self._build_indeed_text(job),
                "authorName": employer.get("name") or "",
                "authorHeadline": location_data.get("countryName") or "",
                "authorProfileUrl": employer.get("companyPageUrl") or "",
                "postedAtISO": job.get("datePublished") or job.get("dateOnIndeed") or "",
                "url": job.get("jobUrl") or job.get("url") or "",
                "title": job.get("title") or "",
                "companyLogo": employer.get("logoUrl") or "",
                "_raw_item_id": job.get("_raw_item_id"),
                "_source": "indeed",
            })

        return normalized

    def _build_glassdoor_text(self, job_data: Dict) -> str:
        """Build a text blob from a raw Glassdoor actor item for the extractor."""
        parts = []
        title = job_data.get("title")
        employer = job_data.get("employer") or {}
        company = employer.get("name")

        location_data = job_data.get("location") or {}
        location = location_data.get("name") if isinstance(location_data, dict) else location_data

        rating = job_data.get("rating")
        easy_apply = job_data.get("easyApply")

        pay = job_data.get("pay") or {}
        salary_str = None
        if pay.get("min") or pay.get("max"):
            currency = pay.get("currency") or ""
            period = pay.get("period") or ""
            salary_str = f"{currency} {pay.get('min') or ''}-{pay.get('max') or ''} per {period}".strip()

        # Glassdoor ships the description as an HTML fragment (no plain-text variant),
        # so strip tags here rather than feeding markup to the LLM.
        description = strip_html(job_data.get("description"))
        apply_url = job_data.get("seoUrl") or job_data.get("url")

        if title:
            parts.append(f"Title: {title}")
        if company:
            parts.append(f"Company: {company}")
        if location:
            parts.append(f"Location: {location}")
        if rating is not None:
            parts.append(f"Employer Rating: {rating}")
        if easy_apply:
            parts.append("Easy Apply: yes")
        if salary_str:
            parts.append(f"Salary: {salary_str}")
        if description:
            parts.append(f"Description: {description[:4000]}")
        if apply_url:
            parts.append(f"Apply here: {apply_url}")

        return "\n".join(parts)

    def _normalize_glassdoor_items(self, glassdoor_items: List[Dict]) -> List[Dict]:
        """Normalize raw Glassdoor actor items into post-like structures for extraction."""
        normalized = []
        for idx, job in enumerate(glassdoor_items):
            # Glassdoor's numeric "id" is what db._item_urn() stores as raw_items.urn, so keep it
            # unprefixed (and stringified the same way) or get_raw_item_by_urn() lookups won't match.
            urn = job.get("id") or job.get("url") or f"glassdoor_{idx}"
            employer = job.get("employer") or {}
            location_data = job.get("location") or {}

            # Actor gives relative age only ("ageInDays"), not an absolute date -- derive one
            # so the UI's "posted at" column has something sortable.
            posted_at = ""
            age_in_days = job.get("ageInDays")
            if age_in_days is not None:
                try:
                    posted_at = (datetime.now() - timedelta(days=int(age_in_days))).isoformat()
                except (TypeError, ValueError):
                    posted_at = ""

            normalized.append({
                "urn": str(urn),
                "text": self._build_glassdoor_text(job),
                "authorName": employer.get("name") or "",
                "authorHeadline": (location_data.get("name") if isinstance(location_data, dict) else location_data) or "",
                "authorProfileUrl": employer.get("url") or "",
                "postedAtISO": posted_at,
                "url": job.get("seoUrl") or job.get("url") or "",
                "title": job.get("title") or "",
                "companyLogo": employer.get("logoUrl") or "",
                "_raw_item_id": job.get("_raw_item_id"),
                "_source": "glassdoor",
            })

        return normalized

    def _build_remote_board_text(self, job_data: Dict, title: str, company: str, location: str, description: str) -> str:
        """Shared text-blob builder for the direct-HTTP remote job boards (Himalayas/RemoteOK/
        Remotive/Jobicy) -- their raw shapes differ but the extractor-facing text format doesn't."""
        parts = []
        if title:
            parts.append(f"Title: {title}")
        if company:
            parts.append(f"Company: {company}")
        if location:
            parts.append(f"Location: {location}")
        if description:
            parts.append(f"Description: {strip_html(description)[:4000]}")
        return "\n".join(parts)

    def _normalize_himalayas_items(self, items: List[Dict]) -> List[Dict]:
        """Normalize raw Himalayas API items into post-like structures for extraction."""
        normalized = []
        for idx, job in enumerate(items):
            urn = job.get("guid") or job.get("applicationLink") or f"himalayas_{idx}"
            title = job.get("title") or ""
            company = job.get("companyName") or ""
            location_restrictions = job.get("locationRestrictions") or []
            location = ", ".join(location_restrictions) if location_restrictions else "Remote"
            description = job.get("description") or ""

            normalized.append({
                "urn": str(urn),
                "text": self._build_remote_board_text(job, title, company, location, description),
                "authorName": company,
                "authorHeadline": location,
                "authorProfileUrl": job.get("companyWebsite") or "",
                "postedAtISO": job.get("pubDate") or "",
                "url": job.get("applicationLink") or "",
                "title": title,
                "companyLogo": job.get("companyLogo") or "",
                "_raw_item_id": job.get("_raw_item_id"),
                "_source": "himalayas",
            })
        return normalized

    def _normalize_remoteok_items(self, items: List[Dict]) -> List[Dict]:
        """Normalize raw RemoteOK API items into post-like structures for extraction."""
        normalized = []
        for idx, job in enumerate(items):
            urn = job.get("id") or job.get("slug") or f"remoteok_{idx}"
            title = job.get("position") or ""
            company = job.get("company") or ""
            location = job.get("location") or "Remote"
            description = job.get("description") or ""

            normalized.append({
                "urn": str(urn),
                "text": self._build_remote_board_text(job, title, company, location, description),
                "authorName": company,
                "authorHeadline": location,
                "authorProfileUrl": job.get("company_url") or "",
                "postedAtISO": job.get("date") or "",
                "url": job.get("apply_url") or job.get("url") or "",
                "title": title,
                "companyLogo": job.get("company_logo") or job.get("logo") or "",
                "_raw_item_id": job.get("_raw_item_id"),
                "_source": "remoteok",
            })
        return normalized

    def _normalize_remotive_items(self, items: List[Dict]) -> List[Dict]:
        """Normalize raw Remotive API items into post-like structures for extraction."""
        normalized = []
        for idx, job in enumerate(items):
            urn = job.get("id") or job.get("url") or f"remotive_{idx}"
            title = job.get("title") or ""
            company = job.get("company_name") or ""
            location = job.get("candidate_required_location") or "Remote"
            description = job.get("description") or ""

            normalized.append({
                "urn": str(urn),
                "text": self._build_remote_board_text(job, title, company, location, description),
                "authorName": company,
                "authorHeadline": location,
                "authorProfileUrl": job.get("company_logo_url") or "",
                "postedAtISO": job.get("publication_date") or "",
                "url": job.get("url") or "",
                "title": title,
                "companyLogo": job.get("company_logo") or job.get("company_logo_url") or "",
                "_raw_item_id": job.get("_raw_item_id"),
                "_source": "remotive",
            })
        return normalized

    def _normalize_jobicy_items(self, items: List[Dict]) -> List[Dict]:
        """Normalize raw Jobicy API items into post-like structures for extraction."""
        normalized = []
        for idx, job in enumerate(items):
            urn = job.get("id") or job.get("url") or f"jobicy_{idx}"
            title = job.get("jobTitle") or ""
            company = job.get("companyName") or ""
            location = job.get("jobGeo") or "Remote"
            description = job.get("jobDescription") or job.get("jobExcerpt") or ""

            normalized.append({
                "urn": str(urn),
                "text": self._build_remote_board_text(job, title, company, location, description),
                "authorName": company,
                "authorHeadline": location,
                "authorProfileUrl": job.get("companyURL") or "",
                "postedAtISO": job.get("pubDate") or "",
                "url": job.get("url") or "",
                "title": title,
                "companyLogo": job.get("companyLogo") or "",
                "_raw_item_id": job.get("_raw_item_id"),
                "_source": "jobicy",
            })
        return normalized

    def _normalize_arbeitnow_items(self, items: List[Dict]) -> List[Dict]:
        """Normalize raw Arbeitnow API items into post-like structures for extraction."""
        normalized = []
        for idx, job in enumerate(items):
            urn = job.get("slug") or job.get("url") or f"arbeitnow_{idx}"
            title = job.get("title") or ""
            company = job.get("company_name") or ""
            location = job.get("location") or ("Remote" if job.get("remote") else "")
            description = strip_html(job.get("description") or "")

            normalized.append({
                "urn": str(urn),
                "text": self._build_remote_board_text(job, title, company, location, description),
                "authorName": company,
                "authorHeadline": location,
                "authorProfileUrl": "",
                "postedAtISO": str(job.get("created_at") or ""),
                "url": job.get("url") or "",
                "title": title,
                "companyLogo": "",
                "_raw_item_id": job.get("_raw_item_id"),
                "_source": "arbeitnow",
            })
        return normalized

    def _normalize_hackernews_items(self, items: List[Dict]) -> List[Dict]:
        """Normalize raw HN 'who is hiring' comment items into post-like structures. Company/
        title/location are best-effort parsed from the comment's first line (conventionally
        "Company | Title | Location | ...")."""
        normalized = []
        for idx, comment in enumerate(items):
            first_line = comment.get("_hn_first_line") or ""
            parts = [p.strip() for p in first_line.split("|")]
            company = parts[0] if len(parts) > 0 else ""
            title = parts[1] if len(parts) > 1 else first_line
            location = parts[2] if len(parts) > 2 else ""
            comment_id = comment.get("id") or f"hackernews_{idx}"

            normalized.append({
                "urn": str(comment_id),
                "text": self._build_remote_board_text(comment, title, company, location, comment.get("_hn_plain_text") or ""),
                "authorName": company,
                "authorHeadline": location,
                "authorProfileUrl": "",
                "postedAtISO": str(comment.get("time") or ""),
                "url": f"https://news.ycombinator.com/item?id={comment_id}",
                "title": title,
                "companyLogo": "",
                "_raw_item_id": comment.get("_raw_item_id"),
                "_source": "hackernews",
            })
        return normalized

    def _normalize_weworkremotely_items(self, items: List[Dict]) -> List[Dict]:
        """Normalize raw WeWorkRemotely RSS entries into post-like structures for extraction.
        WWR titles are conventionally "Company: Title", split apart here."""
        normalized = []
        for idx, entry in enumerate(items):
            raw_title = entry.get("title") or ""
            company, title = "", raw_title
            if ": " in raw_title:
                company, title = raw_title.split(": ", 1)
            link = entry.get("link") or f"weworkremotely_{idx}"

            normalized.append({
                "urn": str(link),
                "text": self._build_remote_board_text(entry, title, company, "Remote", strip_html(entry.get("summary") or "")),
                "authorName": company,
                "authorHeadline": "Remote",
                "authorProfileUrl": "",
                "postedAtISO": entry.get("published") or "",
                "url": link,
                "title": title,
                "companyLogo": "",
                "_raw_item_id": entry.get("_raw_item_id"),
                "_source": "weworkremotely",
            })
        return normalized

    def _normalize_greenhouse_items(self, items: List[Dict]) -> List[Dict]:
        """Normalize raw Greenhouse board API items into post-like structures for extraction."""
        normalized = []
        for idx, job in enumerate(items):
            urn = job.get("id") or job.get("absolute_url") or f"greenhouse_{idx}"
            title = job.get("title") or ""
            company = job.get("_target_company_slug") or ""
            location_data = job.get("location") or {}
            location = location_data.get("name") if isinstance(location_data, dict) else (location_data or "")
            description = strip_html(job.get("content") or "")

            normalized.append({
                "urn": str(urn),
                "text": self._build_remote_board_text(job, title, company, location, description),
                "authorName": company,
                "authorHeadline": location,
                "authorProfileUrl": job.get("absolute_url") or "",
                "postedAtISO": job.get("updated_at") or "",
                "url": job.get("absolute_url") or "",
                "title": title,
                "companyLogo": "",
                "_raw_item_id": job.get("_raw_item_id"),
                "_source": "greenhouse",
            })
        return normalized

    def _normalize_builtin_items(self, items: List[Dict]) -> List[Dict]:
        """Normalize raw BuiltIn JobPosting JSON-LD items into post-like structures."""
        normalized = []
        for idx, job in enumerate(items):
            urn = job.get("_detail_url") or f"builtin_{idx}"
            title = job.get("title") or ""
            org = job.get("hiringOrganization") or {}
            company = org.get("name") if isinstance(org, dict) else ""
            location_data = job.get("jobLocation") or {}
            address = location_data.get("address") if isinstance(location_data, dict) else {}
            location = (address or {}).get("addressLocality") or "Remote"
            description = strip_html(job.get("description") or "")

            normalized.append({
                "urn": str(urn),
                "text": self._build_remote_board_text(job, title, company, location, description),
                "authorName": company or "",
                "authorHeadline": location,
                "authorProfileUrl": job.get("_detail_url") or "",
                "postedAtISO": job.get("datePosted") or "",
                "url": job.get("_detail_url") or "",
                "title": title,
                "companyLogo": "",
                "_raw_item_id": job.get("_raw_item_id"),
                "_source": "builtin",
            })
        return normalized

    def _normalize_wellfound_items(self, items: List[Dict]) -> List[Dict]:
        """Normalize raw Wellfound job items (JSON-LD or Next.js shape) into post-like
        structures for extraction."""
        normalized = []
        for idx, job in enumerate(items):
            title = job.get("title") or job.get("name") or ""
            org = job.get("hiringOrganization")
            if isinstance(org, dict):
                company = org.get("name", "")
            else:
                startup = job.get("startup") or job.get("company")
                company = startup.get("name", "") if isinstance(startup, dict) else (startup or "")
            location = job.get("location") or "Remote"
            if isinstance(location, dict):
                location = location.get("name", "Remote")
            url = job.get("url") or f"wellfound_{idx}"
            description = strip_html(job.get("description") or "")

            normalized.append({
                "urn": str(url),
                "text": self._build_remote_board_text(job, title, company, location, description),
                "authorName": company,
                "authorHeadline": location if isinstance(location, str) else "Remote",
                "authorProfileUrl": url,
                "postedAtISO": job.get("datePosted") or job.get("postedAt") or "",
                "url": url,
                "title": title,
                "companyLogo": "",
                "_raw_item_id": job.get("_raw_item_id"),
                "_source": "wellfound",
            })
        return normalized

    def _normalize_dice_items(self, items: List[Dict]) -> List[Dict]:
        """Normalize raw Dice job items into post-like structures for extraction."""
        normalized = []
        for idx, job in enumerate(items):
            urn = job.get("id") or job.get("guid") or f"dice_{idx}"
            title = job.get("title") or ""
            company = job.get("companyName") or ""
            loc = job.get("jobLocation") or {}
            location_parts = [p for p in [loc.get("city"), loc.get("region")] if p]
            location = ", ".join(location_parts) or "Remote"
            description = job.get("summary") or ""
            detail_url = job.get("detailsPageUrl") or ""

            normalized.append({
                "urn": str(urn),
                "text": self._build_remote_board_text(job, title, company, location, description),
                "authorName": company,
                "authorHeadline": location,
                "authorProfileUrl": detail_url,
                "postedAtISO": job.get("postedDate") or "",
                "url": detail_url,
                "title": title,
                "companyLogo": "",
                "_raw_item_id": job.get("_raw_item_id"),
                "_source": "dice",
            })
        return normalized

    def _normalize_adzuna_items(self, items: List[Dict]) -> List[Dict]:
        """Normalize raw Adzuna API items into post-like structures for extraction."""
        normalized = []
        for idx, job in enumerate(items):
            urn = job.get("id") or job.get("redirect_url") or f"adzuna_{idx}"
            title = job.get("title") or ""
            company = (job.get("company") or {}).get("display_name") or ""
            location = (job.get("location") or {}).get("display_name") or ""
            description = job.get("description") or ""

            normalized.append({
                "urn": str(urn),
                "text": self._build_remote_board_text(job, title, company, location, description),
                "authorName": company,
                "authorHeadline": location,
                "authorProfileUrl": job.get("redirect_url") or "",
                "postedAtISO": job.get("created") or "",
                "url": job.get("redirect_url") or "",
                "title": title,
                "companyLogo": "",
                "_raw_item_id": job.get("_raw_item_id"),
                "_source": "adzuna",
            })
        return normalized

    def _normalize_usajobs_items(self, items: List[Dict]) -> List[Dict]:
        """Normalize raw USAJOBS MatchedObjectDescriptor items into post-like structures."""
        normalized = []
        for idx, job in enumerate(items):
            urn = job.get("PositionURI") or f"usajobs_{idx}"
            title = job.get("PositionTitle") or ""
            company = job.get("OrganizationName") or ""
            location_list = job.get("PositionLocation") or []
            location = location_list[0].get("LocationName", "Remote") if location_list else "Remote"
            user_area = job.get("UserArea") or {}
            duties = (user_area.get("Details") or {}).get("MajorDuties") or [""]
            description = duties[0] if duties else ""

            normalized.append({
                "urn": str(urn),
                "text": self._build_remote_board_text(job, title, company, location, description),
                "authorName": company,
                "authorHeadline": location,
                "authorProfileUrl": job.get("PositionURI") or "",
                "postedAtISO": job.get("PublicationStartDate") or "",
                "url": job.get("PositionURI") or "",
                "title": title,
                "companyLogo": "",
                "_raw_item_id": job.get("_raw_item_id"),
                "_source": "usajobs",
            })
        return normalized

    # Maps a _scraper_source tag to the normalizer method that turns its raw items into
    # post-like dicts. "posts" is intentionally absent -- posts are already post-shaped and
    # pass through untouched. Extend this dict (+ ScraperRegistry.register) to add a new source.
    NORMALIZER_METHODS = {
        "jobs": "_normalize_job_items",
        "indeed": "_normalize_indeed_items",
        "glassdoor": "_normalize_glassdoor_items",
        "himalayas": "_normalize_himalayas_items",
        "remoteok": "_normalize_remoteok_items",
        "remotive": "_normalize_remotive_items",
        "jobicy": "_normalize_jobicy_items",
        "arbeitnow": "_normalize_arbeitnow_items",
        "hackernews": "_normalize_hackernews_items",
        "weworkremotely": "_normalize_weworkremotely_items",
        "greenhouse": "_normalize_greenhouse_items",
        "builtin": "_normalize_builtin_items",
        "wellfound": "_normalize_wellfound_items",
        "dice": "_normalize_dice_items",
        "adzuna": "_normalize_adzuna_items",
        "usajobs": "_normalize_usajobs_items",
    }

    def normalize_bucketed_items(self, items: List[Dict]) -> List[Dict]:
        """Group raw items by their _scraper_source/_source tag and normalize each bucket with
        its matching method (see NORMALIZER_METHODS) -- one call site instead of a repetitive
        per-source if/extend block, so adding a new source doesn't touch this method at all.
        """
        buckets: Dict[str, List[Dict]] = {}
        for item in items:
            src = item.get('_scraper_source') or item.get('_source') or 'posts'
            buckets.setdefault(src, []).append(item)

        normalized = list(buckets.get('posts', []))
        for source, method_name in self.NORMALIZER_METHODS.items():
            bucket = buckets.get(source)
            if bucket:
                normalized.extend(getattr(self, method_name)(bucket))
        return normalized

    def scrape_data(self, search_urls: List[str], limit_per_source: int = 50) -> str:
        """
        Scrape LinkedIn data
        """
        logging.info("🔍 Starting LinkedIn data scraping...")

        if not self.scraper:
            self._initialize_scraper()

        try:
            data_file = self.scraper.scrape_linkedin_posts(search_urls, limit_per_source)
            logging.info(f"✅ Scraping completed: {data_file}")
            return data_file
        except Exception as e:
            logging.error(f"❌ Scraping failed: {e}")
            raise

    def scrape_job_data(self, job_input: Dict, max_items: Optional[int] = None) -> str:
        """
        Scrape LinkedIn jobs data
        """
        logging.info("🔍 Starting LinkedIn job search scraping...")

        if not self.scraper:
            self._initialize_scraper()

        try:
            data_file = self.scraper.scrape_linkedin_jobs(job_input, max_items=max_items)
            logging.info(f"✅ Job scraping completed: {data_file}")
            return data_file
        except Exception as e:
            logging.error(f"❌ Job scraping failed: {e}")
            raise

    def extract_leads_from_posts(self, posts_data: List[Dict]) -> Tuple[List[ExtractedLead], Dict]:
        """
        Extract leads from in-memory post/job data
        """
        logging.info("🔬 Starting lead extraction...")

        if not self.extractor:
            self._initialize_extractor()

        if not self.cold_email_system:
            self._initialize_email_system()

        logging.info(f"Loaded {len(posts_data)} items for extraction")

        leads, stats = self.cold_email_system.process_and_prepare_emails(posts_data)
        logging.info(f"✅ Extraction completed: {len(leads)} leads extracted")
        return leads, stats

    def extract_leads(self, data_file: str) -> Tuple[List[ExtractedLead], Dict]:
        """
        Extract leads from scraped data
        """
        if not os.path.exists(data_file):
            raise FileNotFoundError(f"Data file {data_file} not found")

        with open(data_file, 'r', encoding='utf-8') as f:
            posts_data = json.load(f)
        logging.info(f"Loaded {len(posts_data)} posts from {data_file}")

        return self.extract_leads_from_posts(posts_data)

    def run_complete_workflow(self,
                            search_urls: List[str],
                            limit_per_source: int = 50,
                            skip_scraping: bool = False,
                            existing_data_file: str = None,
                            include_jobs: bool = False,
                            jobs_only: bool = False,
                            job_input: Optional[Dict] = None,
                            include_indeed: bool = False,
                            indeed_input: Optional[Dict] = None,
                            include_glassdoor: bool = False,
                            glassdoor_input: Optional[Dict] = None,
                            include_remote_boards: bool = False,
                            extra_scrapers: Optional[List[str]] = None,
                            target_companies: Optional[List[str]] = None) -> Tuple[List[ExtractedLead], Dict]:
        """
        Run the complete workflow: scrape -> extract -> email
        """
        logging.info("🚀 Starting complete LinkedIn lead generation workflow")
        logging.info("=" * 60)

        # Display configuration
        logging.info("⚙️ Configuration:")
        logging.info(f"  Auto-email: {self.config_manager.is_auto_email_enabled()}")
        logging.info(f"  Parallel extraction: {self.config_manager.get('enable_parallel_extraction', True)}")
        logging.info(f"  Max posts per key: {self.config_manager.get('max_posts_per_key', 250)}")

        all_posts = []

        # Step 1: Scrape dynamically if requested
        if not skip_scraping:
            search_params = {
                "search_urls": search_urls,
                "limit": limit_per_source,
                "job_input": job_input,
                "indeed_input": indeed_input,
                "glassdoor_input": glassdoor_input,
                "target_companies": target_companies or [],
            }

            enabled_scrapers = []
            if not jobs_only:
                enabled_scrapers.append("posts")
            if include_jobs or jobs_only:
                enabled_scrapers.append("jobs")
            if include_indeed:
                enabled_scrapers.append("indeed")
            if include_glassdoor:
                enabled_scrapers.append("glassdoor")
            if include_remote_boards:
                enabled_scrapers.extend(["himalayas", "remoteok", "remotive", "jobicy"])
            enabled_scrapers.extend(extra_scrapers or [])

            logging.info(f"Triggering scraper registry execution: {enabled_scrapers}")
            raw_results = scraper_registry.scrape_all(self, search_params, enabled_scrapers)

            # Normalize exactly once, here -- posts are already post-shaped; every other source
            # is raw actor/API output at this point and needs shaping into post-like dicts.
            all_posts = self.normalize_bucketed_items(raw_results)
        else:
            # Fallback to loading existing local data files
            if not jobs_only:
                posts_file = existing_data_file or "linkedin_data.json"
                if os.path.exists(posts_file):
                    logging.info(f"📁 Loading existing posts data file: {posts_file}")
                    with open(posts_file, 'r', encoding='utf-8') as f:
                        all_posts.extend(json.load(f))

            if include_jobs or jobs_only:
                jobs_file = "linkedin_jobs.json"
                if os.path.exists(jobs_file):
                    logging.info(f"📁 Loading existing jobs data file: {jobs_file}")
                    with open(jobs_file, 'r', encoding='utf-8') as f:
                        job_items = json.load(f)
                    all_posts.extend(self._normalize_job_items(job_items))

            if include_indeed:
                indeed_file = "indeed_jobs.json"
                if os.path.exists(indeed_file):
                    logging.info(f"📁 Loading existing Indeed jobs data file: {indeed_file}")
                    with open(indeed_file, 'r', encoding='utf-8') as f:
                        indeed_items = json.load(f)
                    all_posts.extend(self._normalize_indeed_items(indeed_items))

            if include_glassdoor:
                glassdoor_file = "glassdoor_jobs.json"
                if os.path.exists(glassdoor_file):
                    logging.info(f"📁 Loading existing Glassdoor jobs data file: {glassdoor_file}")
                    with open(glassdoor_file, 'r', encoding='utf-8') as f:
                        glassdoor_items = json.load(f)
                    all_posts.extend(self._normalize_glassdoor_items(glassdoor_items))

        # Step 2: Extraction and Email Processing
        leads, stats = self.extract_leads_from_posts(all_posts)

        # Step 3: Summary
        logging.info("\n🎯 WORKFLOW SUMMARY:")
        logging.info("=" * 40)
        logging.info(f"Total leads extracted: {len(leads)}")

        if stats.get("auto_email_enabled"):
            logging.info(f"Emails sent: {stats.get('emails_sent', 0)}")
            logging.info(f"Emails skipped: {stats.get('emails_skipped', 0)}")
        else:
            logging.info("Emails prepared but not sent (auto-email disabled)")

        logging.info("✅ Workflow completed successfully!")

        return leads, stats

def create_default_search_urls() -> List[str]:
    """Create default LinkedIn search URLs for internships and Python roles"""
    base_urls = [
        # Internship + Python + India (last 24 hours)
        "https://www.linkedin.com/search/results/content/?datePosted=%22past-24h%22&keywords=%22intern%22%20and%20%22python%22%20and%20%22india%22&origin=FACETED_SEARCH&sortBy=%22relevance%22",

        # Fresher + Software Developer (last 24 hours)
        #"https://www.linkedin.com/search/results/content/?datePosted=%22past-24h%22&keywords=%22fresher%22%20and%20%22software%20developer%22&origin=FACETED_SEARCH&sortBy=%22relevance%22",

        # Entry level + AI/ML (last 24 hours)
       # "https://www.linkedin.com/search/results/content/?datePosted=%22past-24h%22&keywords=%22entry%20level%22%20and%20%22machine%20learning%22&origin=FACETED_SEARCH&sortBy=%22relevance%22",

        # 2026 batch + hiring (last 24 hours)
        #"https://www.linkedin.com/search/results/content/?datePosted=%22past-24h%22&keywords=%222026%20batch%22%20and%20%22hiring%22&origin=FACETED_SEARCH&sortBy=%22relevance%22"
    ]

    return base_urls

def create_default_job_search_input() -> Dict:
    """Create default job search input for the LinkedIn jobs actor."""
    return {
        "startUrls": [
            {
                "url": "https://www.linkedin.com/jobs/search/?f_TPR=r86400&geoId=102713980&keywords=data+scientist"
            }
        ],
        "keyword": ["Software Engineer"],
        "location": "India",
        "distance": "",
        "publishedAt": "r86400",
        "jobType": [],
        "experienceLevel": [],
        "workType": [],
        "salaryBase": "",
        "maxItems": 150,
        "saveOnlyUniqueItems": False,
        "enrichCompanyData": False,
        "resumeKeywords": [
            {"keyword": "JavaScript", "aliases": ["JS"]},
            {"keyword": "TypeScript", "aliases": ["TS"]},
            {"keyword": "Node.js", "aliases": ["Node", "NodeJS"]},
            {"keyword": "React Native", "aliases": ["RN"]},
            {"keyword": "React"},
            {"keyword": "Expo"},
        ],
    }

def create_default_indeed_search_input() -> Dict:
    """Create default Indeed job search input, focused on India."""
    return {
        "country": "in",
        "title": "Software Engineer",
        "location": "India",
        "limit": 50,
        "datePosted": "1",
    }

def create_default_glassdoor_search_input() -> Dict:
    """Create default Glassdoor job search input, focused on India.

    The actor resolves 'location' as free text against Glassdoor's own place index, and a bare
    "India" resolves to *Indiana, US* -- naming a city pins it to the right country (verified:
    "Bengaluru, India" returns countryId 115).
    """
    return {
        "keywords": "Software Engineer",
        "location": "Bengaluru, India",
        "daysOld": 1,
        "easyApply": False,
        "sortBy": "date_desc",
        "limit": 50,
    }

def main():
    """Main function with command line interface"""
    import argparse

    parser = argparse.ArgumentParser(description="LinkedIn Lead Generation Runner")
    # parser.add_argument("--config", default="/home/Lazycat/mysite/configs/config.json", help="Configuration file path")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Configuration file path")
    parser.add_argument("--skip-scraping", action="store_true", help="Skip scraping, use existing data")
    parser.add_argument("--data-file", help="Existing data file to use (if skip-scraping)")
    parser.add_argument("--limit", type=int, default=50, help="Posts to scrape per URL")
    parser.add_argument("--urls-file", help="JSON file containing search URLs")
    parser.add_argument("--scrape-jobs", action="store_true", help="Also scrape LinkedIn jobs")
    parser.add_argument("--jobs-only", action="store_true", help="Scrape LinkedIn jobs only (skip posts)")
    parser.add_argument("--jobs-input", help="JSON file containing jobs actor input")
    parser.add_argument("--scrape-indeed", action="store_true", help="Also scrape Indeed jobs")
    parser.add_argument("--indeed-input", help="JSON file containing Indeed actor input")
    parser.add_argument("--scrape-glassdoor", action="store_true", help="Also scrape Glassdoor jobs")
    parser.add_argument("--glassdoor-input", help="JSON file containing Glassdoor actor input")
    parser.add_argument("--scrape-remote-boards", action="store_true",
                         help="Also scrape Himalayas/RemoteOK/Remotive/Jobicy (last 24h only)")
    parser.add_argument("--extra-scrapers", help="Comma-separated extra scraper names "
                         "(arbeitnow,hackernews,weworkremotely,greenhouse,builtin,wellfound,dice,adzuna,usajobs)")
    parser.add_argument("--target-companies", help="Comma-separated Greenhouse board slugs (only used by --extra-scrapers=greenhouse)")
    parser.add_argument("--dry-run", action="store_true", help="Run without sending emails")

    args = parser.parse_args()

    # Temporarily disable auto-email for dry run
    if args.dry_run:
        logging.info("🧪 Dry run mode: emails will not be sent")
        # Load config and temporarily disable auto-email
        with open(args.config, 'r') as f:
            config = json.load(f)
        config["auto_email"] = False
        with open(args.config, 'w') as f:
            json.dump(config, f, indent=2)

    try:
        # Initialize runner
        runner = LinkedInRunner(args.config)

        # Load search URLs
        if args.urls_file and os.path.exists(args.urls_file):
            with open(args.urls_file, 'r') as f:
                search_urls = json.load(f)
            logging.info(f"Loaded {len(search_urls)} URLs from {args.urls_file}")
        else:
            search_urls = create_default_search_urls()
            logging.info(f"Using {len(search_urls)} default search URLs")

        job_input = None
        if args.jobs_input and os.path.exists(args.jobs_input):
            with open(args.jobs_input, 'r', encoding='utf-8') as f:
                job_input = json.load(f)

        indeed_input = None
        if args.indeed_input and os.path.exists(args.indeed_input):
            with open(args.indeed_input, 'r', encoding='utf-8') as f:
                indeed_input = json.load(f)

        glassdoor_input = None
        if args.glassdoor_input and os.path.exists(args.glassdoor_input):
            with open(args.glassdoor_input, 'r', encoding='utf-8') as f:
                glassdoor_input = json.load(f)

        # Run workflow
        leads, stats = runner.run_complete_workflow(
            search_urls=search_urls,
            limit_per_source=args.limit,
            skip_scraping=args.skip_scraping,
            existing_data_file=args.data_file,
            include_jobs=args.scrape_jobs or args.jobs_only,
            jobs_only=args.jobs_only,
            job_input=job_input,
            include_indeed=args.scrape_indeed,
            indeed_input=indeed_input,
            include_glassdoor=args.scrape_glassdoor,
            glassdoor_input=glassdoor_input,
            include_remote_boards=args.scrape_remote_boards,
            extra_scrapers=[s.strip() for s in args.extra_scrapers.split(",") if s.strip()] if args.extra_scrapers else None,
            target_companies=[c.strip() for c in args.target_companies.split(",") if c.strip()] if args.target_companies else None
        )

        # Display final results
        if leads:
            logging.info(f"\n🎉 SUCCESS: Generated {len(leads)} leads")

            # Show key statistics
            email_leads = [l for l in leads if l.application_method == 'email' and l.application_contact]
            manual_leads = [l for l in leads if l.application_method != 'email' or not l.application_contact]

            logging.info(f"📧 Email-ready leads: {len(email_leads)}")
            logging.info(f"📝 Manual follow-up leads: {len(manual_leads)}")

            # Show top companies
            companies = {}
            for lead in leads:
                company = lead.company_name or "Unknown"
                companies[company] = companies.get(company, 0) + 1

            top_companies = sorted(companies.items(), key=lambda x: x[1], reverse=True)[:5]
            logging.info(f"\n🏢 Top companies:")
            for company, count in top_companies:
                logging.info(f"  {company}: {count} positions")
        else:
            logging.warning("❌ No leads generated")

        return 0

    except KeyboardInterrupt:
        logging.info("\n⏹️  Workflow interrupted by user")
        return 1
    except Exception as e:
        logging.error(f"❌ Workflow failed: {e}")
        return 1

if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)