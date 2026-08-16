#!/usr/bin/env python3
"""Standalone cron entrypoint for the full scrape -> LLM extraction -> DB pipeline.

Run directly, no Flask server required:
    python cron_pipeline.py
    python cron_pipeline.py --limit 100 --extra-scrapers arbeitnow,hackernews,greenhouse

Mirrors server.py's execute_pipeline_background() (used by the `/api/scrape` route) minus the
Flask-specific pipeline_status tracking and background threading -- cron itself is the
"background", so this just runs once, synchronously, and exits. If the two ever need to diverge,
keep behavior identical here and there; run_extraction_for_user is imported (not duplicated) from
server.py specifically to avoid the two pipelines drifting apart.
"""
import argparse
import json
import logging
import sys
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

from script import LinkedInRunner, create_default_search_urls, scraper_registry
import db as db_store
from server import run_extraction_for_user, run_global_extraction, USERS_DIR

DEFAULT_EXTRA_SCRAPERS = "arbeitnow,hackernews,weworkremotely,greenhouse,wellfound,adzuna"


def run_pipeline(limit, scrape_jobs, scrape_indeed, indeed_input, scrape_glassdoor, glassdoor_input,
                  scrape_remote_boards, extra_scrapers, search_urls=None):
    logging.info("Cron pipeline run starting")

    global_runner = LinkedInRunner()
    # Union of every user's target companies -- Greenhouse has no cross-company search, each
    # company's postings live at their own board token (see script.py's GreenhouseScraper).
    all_target_companies = db_store.get_all_target_companies()
    search_params = {
        "search_urls": search_urls or create_default_search_urls(),
        "limit": limit,
        "indeed_input": indeed_input,
        "glassdoor_input": glassdoor_input,
        "target_companies": all_target_companies,
    }

    scrapers_to_run = ["posts"]
    if scrape_jobs:
        scrapers_to_run.append("jobs")
    if scrape_indeed:
        scrapers_to_run.append("indeed")
    if scrape_glassdoor:
        scrapers_to_run.append("glassdoor")
    if scrape_remote_boards:
        scrapers_to_run.extend(["himalayas", "remoteok", "remotive", "jobicy"])
    for name in (extra_scrapers or []):
        if name == "greenhouse" and not all_target_companies:
            logging.info("Skipping greenhouse: no user has configured any target companies")
            continue
        scrapers_to_run.append(name)

    logging.info(f"Running scrapers: {scrapers_to_run}")
    raw_items = scraper_registry.scrape_all(global_runner, search_params, scrapers_to_run)

    newly_stored = db_store.insert_raw_items(raw_items, source="posts")
    logging.info(f"Scraping finished. Got {len(raw_items)} raw items ({newly_stored} new).")

    global_metrics = run_global_extraction()
    logging.info(f"Global extraction metrics: {global_metrics}")
    total_extraction_attempts = 0
    if USERS_DIR.exists():
        user_folders = [f.name for f in USERS_DIR.iterdir() if f.is_dir()]
        logging.info(f"Running cached user matching for users: {user_folders}")
        for user_id in user_folders:
            try:
                count = run_extraction_for_user(user_id)
                total_extraction_attempts += count
                logging.info(f"User {user_id}: {count} raw item(s) attempted")
            except Exception as e:
                logging.error(f"Extraction failed for user {user_id}: {e}")

    result = {
        "finished_at": datetime.now().isoformat(),
        "raw_items_scraped": len(raw_items),
        "raw_items_new": newly_stored,
        "extraction_attempts": total_extraction_attempts,
        "global_extraction": global_metrics,
        "scrapers_run": scrapers_to_run,
    }
    logging.info(f"Cron pipeline run complete: {result}")
    return result


def main():
    parser = argparse.ArgumentParser(description="Run the scrape + LLM extraction pipeline once (for cron).")
    parser.add_argument("--limit", type=int, default=50, help="Items per source (default: 50)")
    parser.add_argument("--scrape-jobs", dest="scrape_jobs", action="store_true", default=True)
    parser.add_argument("--no-scrape-jobs", dest="scrape_jobs", action="store_false")
    parser.add_argument("--scrape-indeed", action="store_true", default=False)
    parser.add_argument("--scrape-glassdoor", action="store_true", default=False)
    parser.add_argument("--scrape-remote-boards", dest="scrape_remote_boards", action="store_true", default=True)
    parser.add_argument("--no-scrape-remote-boards", dest="scrape_remote_boards", action="store_false")
    parser.add_argument(
        "--extra-scrapers", type=str, default=DEFAULT_EXTRA_SCRAPERS,
        help=f"Comma-separated scraper names to also run (default: {DEFAULT_EXTRA_SCRAPERS})",
    )
    parser.add_argument(
        "--status-file", type=str, default=None,
        help="Optional path to write a JSON summary of the run (for external cron monitoring)",
    )
    args = parser.parse_args()

    extra_scrapers = [s.strip() for s in args.extra_scrapers.split(",") if s.strip()]

    try:
        result = run_pipeline(
            limit=args.limit,
            scrape_jobs=args.scrape_jobs,
            scrape_indeed=args.scrape_indeed,
            indeed_input=None,
            scrape_glassdoor=args.scrape_glassdoor,
            glassdoor_input=None,
            scrape_remote_boards=args.scrape_remote_boards,
            extra_scrapers=extra_scrapers,
        )
        if args.status_file:
            with open(args.status_file, "w", encoding="utf-8") as f:
                json.dump({**result, "success": True}, f, indent=2)
        return 0
    except Exception as e:
        logging.error(f"Cron pipeline run failed: {e}")
        if args.status_file:
            with open(args.status_file, "w", encoding="utf-8") as f:
                json.dump({"success": False, "error": str(e), "finished_at": datetime.now().isoformat()}, f, indent=2)
        return 1


if __name__ == "__main__":
    sys.exit(main())
