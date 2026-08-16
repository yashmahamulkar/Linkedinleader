import unittest
import json
from pathlib import Path
from unittest.mock import patch

import db


class GlobalPipelineStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(__file__).parent / "_test_db"
        self.tmp.mkdir(exist_ok=True)
        db.DB_DIR = self.tmp
        db.DB_PATH = db.DB_DIR / "test.db"
        if db.DB_PATH.exists():
            db.DB_PATH.unlink()
        db._initialized = False
        db.init_db()

    def tearDown(self):
        db._initialized = False
        if db.DB_PATH.exists():
            db.DB_PATH.unlink()

    def test_same_raw_item_can_be_claimed_only_once(self):
        item = {"urn": "urn:post:1", "text": "Hiring Python intern"}
        self.assertEqual(db.insert_raw_items([item], "posts"), 1)
        candidate = db.get_global_extraction_candidates()[0]
        self.assertTrue(db.claim_global_extraction(candidate["_raw_item_id"], candidate["_urn"]))
        self.assertFalse(db.claim_global_extraction(candidate["_raw_item_id"], candidate["_urn"]))
        db.save_global_extraction(candidate["_raw_item_id"], candidate["_urn"], {"post_urn": candidate["_urn"]})
        self.assertEqual(db.get_global_extraction_candidates(), [])

    def test_duplicate_scrape_does_not_duplicate_raw_item(self):
        item = {"urn": "urn:post:2", "text": "Hiring"}
        self.assertEqual(db.insert_raw_items([item], "posts"), 1)
        self.assertEqual(db.insert_raw_items([item], "posts"), 0)

    def test_global_extraction_is_shared_by_multiple_users(self):
        item = {"urn": "urn:post:3", "text": "Hiring"}
        db.insert_raw_items([item], "posts")
        candidate = db.get_global_extraction_candidates()[0]
        self.assertTrue(db.claim_global_extraction(candidate["_raw_item_id"], candidate["_urn"]))
        payload = {"post_urn": candidate["_urn"], "is_job_posting": True}
        db.save_global_extraction(candidate["_raw_item_id"], candidate["_urn"], payload)
        self.assertEqual(len(db.get_completed_global_extractions()), 1)

    def test_pipeline_calls_global_extractor_once_and_never_user_filter(self):
        import server

        item = {"urn": "urn:post:shared", "text": "We are hiring a Python intern"}
        db.insert_raw_items([item], "posts")
        calls = []

        class FakeExtractor:
            def extract_single_post_globally(self, post):
                calls.append(post["_urn"])
                from extractor import ExtractedLead
                return ExtractedLead(
                    post_urn=post["_urn"], application_method="other", posting_date="",
                    author_name="", author_profile="", is_job_posting=True,
                    post_category="job", job_title="Python Intern", location="Remote",
                    is_internship=True, is_fresher=True,
                ), "GLOBAL_EXTRACTED"

            def process_single_post(self, post):
                raise AssertionError("global pipeline must not use user-dependent extraction")

        class FakeRunner:
            def normalize_bucketed_items(self, items):
                return items

            def _initialize_extractor(self):
                self.extractor = FakeExtractor()

        with patch.object(server, "LinkedInRunner", FakeRunner):
            first = server.run_global_extraction()
            second = server.run_global_extraction()

        self.assertEqual(calls, ["urn:post:shared"])
        self.assertEqual(first["llm_successes"], 1)
        self.assertEqual(second["candidates"], 0)
        self.assertGreaterEqual(second["already_extracted"], 1)

    def test_prefiltered_item_is_cached_as_skipped(self):
        import server

        db.insert_raw_items([{"urn": "urn:post:empty", "text": ""}], "posts")
        with patch.object(server, "LinkedInRunner"):
            first = server.run_global_extraction()
            second = server.run_global_extraction()
        self.assertEqual(first["skipped_by_filter"], 1)
        self.assertEqual(second["candidates"], 0)

    def test_structured_source_pipeline_uses_zero_llm_calls(self):
        import server

        item = {
            "urn": "greenhouse-structured-1", "_scraper_source": "greenhouse",
            "title": "Python Intern", "companyName": "Example", "location": "Remote",
            "url": "https://example.test/apply", "text": "Python Intern at Example",
        }
        db.insert_raw_items([item], "posts")

        class FakeRunner:
            def normalize_bucketed_items(self, items):
                return items

            def _initialize_extractor(self):
                raise AssertionError("structured sources must not initialize an LLM")

        with patch.object(server, "LinkedInRunner", FakeRunner):
            metrics = server.run_global_extraction()

        self.assertEqual(metrics["structured_items"], 1)
        self.assertEqual(metrics["llm_items"], 0)

    def test_failed_global_extraction_remains_retryable(self):
        import server

        db.insert_raw_items([{"urn": "urn:post:retry", "text": "Hiring Python developer"}], "posts")
        calls = []

        class FakeExtractor:
            def extract_single_post_globally(self, post):
                calls.append(post["_urn"])
                return None, "EXTRACTION_ERROR"

        class FakeRunner:
            def normalize_bucketed_items(self, items):
                return items

            def _initialize_extractor(self):
                self.extractor = FakeExtractor()

        with patch.object(server, "LinkedInRunner", FakeRunner):
            first = server.run_global_extraction()
            second = server.run_global_extraction()

        self.assertEqual(first["llm_failures"], 1)
        self.assertEqual(second["llm_failures"], 1)
        self.assertEqual(calls, ["urn:post:retry", "urn:post:retry"])

    def test_structured_source_does_not_require_llm(self):
        from server import _structured_lead
        lead = _structured_lead({
            "urn": "greenhouse-1", "_source": "greenhouse", "title": "Python Intern",
            "authorName": "Example", "authorHeadline": "Remote", "url": "https://example.test/apply",
            "text": "Python Intern at Example. Apply here."
        })
        self.assertEqual(lead.application_method, "link")
        self.assertTrue(lead.is_internship)
        self.assertEqual(lead.company_name, "Example")

    def test_user_matching_is_independent_for_each_user_and_preferences(self):
        from extractor import ExtractedLead
        from preferencemanager import PreferenceManager
        from server import _lead_matches_user

        lead = ExtractedLead(
            post_urn="shared", application_method="link", posting_date="", author_name="Example",
            author_profile="", is_job_posting=True, post_category="job", job_title="Python Intern",
            company_name="Example", location="Remote", work_mode="Remote", is_internship=True,
            is_fresher=True,
        )
        first = Path(self.tmp) / "a.json"
        second = Path(self.tmp) / "b.json"
        first.write_text(json.dumps({"preferred_roles": ["Python"], "preferred_locations": ["Remote"], "categories": {}}))
        second.write_text(json.dumps({"preferred_roles": ["Marketing"], "preferred_locations": ["Remote"], "categories": {}}))
        self.assertTrue(_lead_matches_user(lead, PreferenceManager(preferences_path=str(first)))[0])
        self.assertFalse(_lead_matches_user(lead, PreferenceManager(preferences_path=str(second)))[0])

    def test_fresher_matching_rejects_sales_and_senior_roles_even_without_role_level(self):
        from extractor import ExtractedLead
        from preferencemanager import PreferenceManager
        from server import _lead_matches_user

        pref_path = Path(self.tmp) / "fresher.json"
        pref_path.write_text(json.dumps({
            "preferred_roles": ["Software Developer", "AI/ML Engineer"],
            "preferred_locations": ["Remote"], "categories": {},
        }))
        pref = PreferenceManager(preferences_path=str(pref_path))

        for title, years, role_level in (("Sales Manager", [0, 0], None),
                                         ("Principal Software Engineer", [0, 0], "Principal"),
                                         ("Backend Engineer", [5, 8], None)):
            lead = ExtractedLead(
                post_urn=title, application_method="link", posting_date="", author_name="",
                author_profile="", is_job_posting=True, post_category="job",
                job_title=title, company_name="Example", location="Remote",
                experience_level=years, role_level=role_level,
            )
            self.assertFalse(_lead_matches_user(lead, pref)[0], title)


if __name__ == "__main__":
    unittest.main()
