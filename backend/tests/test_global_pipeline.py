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


if __name__ == "__main__":
    unittest.main()
