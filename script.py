#!/usr/bin/env python3
"""
LinkedIn Lead Generation Runner
Integrates scraping, extraction, and email sending in a complete workflow
"""

import json
import os
import sys
import logging
from datetime import datetime
from typing import List, Dict, Tuple, Optional
from pathlib import Path

# Import scraper
from apify_client import ApifyClient

from config import ConfigManager
from keymanager import KeyManager
from preferencemanager import PreferenceManager
from emailmanager import TemplateLoader, EmailTemplateGenerator, ColdEmailSystem
from extractor import LinkedInLeadExtractor, ExtractedLead
from datetime import datetime

class LinkedInScraper:
    """LinkedIn scraper with key management integration"""

    def __init__(self, config_manager: ConfigManager):
        self.config_manager = ConfigManager('/home/Lazycat/mysite/configs/config.json')
        scraper_keys_path = config_manager.get("scraper_keys_path", "/home/Lazycat/mysite/configs/scraper_keys.json")
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

            # Fetch Actor results
            results = []
            for item in self.client.dataset(run["defaultDatasetId"]).iterate_items():
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

class LinkedInRunner:
    """Main runner class that orchestrates the complete workflow"""

    def __init__(self, config_path: str = "/home/Lazycat/mysite/configs/config.json"):
        self.config_manager = ConfigManager(config_path)
        self.preference_manager = PreferenceManager()
        self.template_loader = TemplateLoader()

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
            gemini_keys_path = self.config_manager.get("gemini_keys_path", "gemini_keys.json")
            #preferred_roles = self.preference_manager.preferred_roles()
            #preferred_locations = self.preference_manager.preferred_locations()
            preferred_roles = ["Software Developer", "AI/ML Engineer"],
            preferred_locations =["Remote", "Mumnbai"]
            self.extractor = LinkedInLeadExtractor(
                gemini_keys_path=gemini_keys_path,
                preferred_roles=preferred_roles,
                preferred_locations=preferred_locations,
                strict_filtering=True,
                config_manager=self.config_manager
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
                resume_path=self.config_manager.get("resume_path", "path/to/resume.pdf"),
                template_loader=self.template_loader
            )

            self.cold_email_system = ColdEmailSystem(
                self.extractor,
                self.email_generator,
                self.config_manager
            )
            logging.info("✅ Email system initialized successfully")
        except Exception as e:
            logging.error(f"❌ Failed to initialize email system: {e}")
            raise

    def scrape_data(self, search_urls: List[str], limit_per_source: int = 50) -> str:
        """
        Scrape LinkedIn data

        Args:
            search_urls: List of LinkedIn search URLs to scrape
            limit_per_source: Number of posts to scrape per URL

        Returns:
            Path to the scraped data file
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

    def extract_leads(self, data_file: str) -> Tuple[List[ExtractedLead], Dict]:
        """
        Extract leads from scraped data

        Args:
            data_file: Path to the JSON file containing scraped LinkedIn data

        Returns:
            Tuple of (extracted leads, statistics)
        """
        logging.info("🔬 Starting lead extraction...")

        if not self.extractor:
            self._initialize_extractor()

        if not self.cold_email_system:
            self._initialize_email_system()

        # Load scraped data
        if not os.path.exists(data_file):
            raise FileNotFoundError(f"Data file {data_file} not found")

        with open(data_file, 'r', encoding='utf-8') as f:
            posts_data = json.load(f)

        logging.info(f"Loaded {len(posts_data)} posts from {data_file}")

        # Process and extract leads
        leads, stats = self.cold_email_system.process_and_prepare_emails(posts_data)

        logging.info(f"✅ Extraction completed: {len(leads)} leads extracted")
        return leads, stats

    def run_complete_workflow(self,
                            search_urls: List[str],
                            limit_per_source: int = 50,
                            skip_scraping: bool = False,
                            existing_data_file: str = None) -> Tuple[List[ExtractedLead], Dict]:
        """
        Run the complete workflow: scrape -> extract -> email

        Args:
            search_urls: LinkedIn search URLs to scrape
            limit_per_source: Posts to scrape per URL
            skip_scraping: If True, skip scraping and use existing data
            existing_data_file: Path to existing data file (if skip_scraping=True)

        Returns:
            Tuple of (extracted leads, statistics)
        """
        logging.info("🚀 Starting complete LinkedIn lead generation workflow")
        logging.info("=" * 60)

        # Display configuration
        logging.info("⚙️ Configuration:")
        logging.info(f"  Auto-email: {self.config_manager.is_auto_email_enabled()}")
        logging.info(f"  Parallel extraction: {self.config_manager.get('enable_parallel_extraction', True)}")
        logging.info(f"  Max posts per key: {self.config_manager.get('max_posts_per_key', 10)}")

        # Step 1: Scraping (optional)
        if skip_scraping:
            if not existing_data_file:
                existing_data_file = "linkedin_data2.json"  # Default fallback

            if not os.path.exists(existing_data_file):
                raise FileNotFoundError(f"Existing data file {existing_data_file} not found")

            logging.info(f"📁 Using existing data file: {existing_data_file}")
            data_file = existing_data_file
        else:
            data_file = self.scrape_data(search_urls, limit_per_source)

        # Step 2: Extraction and Email Processing
        leads, stats = self.extract_leads(data_file)

        # Step 3: Summary
        logging.info("\n🎯 WORKFLOW SUMMARY:")
        logging.info("=" * 40)
        logging.info(f"Data source: {data_file}")
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
        # Internship + Python (last 24 hours)
        "https://www.linkedin.com/search/results/content/?datePosted=%22past-24h%22&keywords=%22intern%22%20and%20%22python%22&origin=FACETED_SEARCH&sortBy=%22relevance%22",

        # Fresher + Software Developer (last 24 hours)
        #"https://www.linkedin.com/search/results/content/?datePosted=%22past-24h%22&keywords=%22fresher%22%20and%20%22software%20developer%22&origin=FACETED_SEARCH&sortBy=%22relevance%22",

        # Entry level + AI/ML (last 24 hours)
       # "https://www.linkedin.com/search/results/content/?datePosted=%22past-24h%22&keywords=%22entry%20level%22%20and%20%22machine%20learning%22&origin=FACETED_SEARCH&sortBy=%22relevance%22",

        # 2026 batch + hiring (last 24 hours)
        #"https://www.linkedin.com/search/results/content/?datePosted=%22past-24h%22&keywords=%222026%20batch%22%20and%20%22hiring%22&origin=FACETED_SEARCH&sortBy=%22relevance%22"
    ]

    return base_urls

def main():
    """Main function with command line interface"""
    import argparse

    parser = argparse.ArgumentParser(description="LinkedIn Lead Generation Runner")
    parser.add_argument("--config", default="/home/Lazycat/mysite/configs/config.json", help="Configuration file path")
    parser.add_argument("--skip-scraping", action="store_true", help="Skip scraping, use existing data")
    parser.add_argument("--data-file", help="Existing data file to use (if skip-scraping)")
    parser.add_argument("--limit", type=int, default=50, help="Posts to scrape per URL")
    parser.add_argument("--urls-file", help="JSON file containing search URLs")
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

        # Run workflow
        leads, stats = runner.run_complete_workflow(
            search_urls=search_urls,
            limit_per_source=args.limit,
            skip_scraping=args.skip_scraping,
            existing_data_file=args.data_file
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