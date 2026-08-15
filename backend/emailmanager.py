from typing import Dict, List, Optional, Union, Tuple
import json
import os
import threading
from pathlib import Path
import logging
from dotenv import load_dotenv
from datetime import datetime, timedelta
from dataclasses import asdict
from keymanager import KeyManager
from config import ConfigManager
from preferencemanager import PreferenceManager
from extractor import LinkedInLeadExtractor, ExtractedLead
import pandas as pd
import db as db_store

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent
# DEFAULT_TEMPLATES_DIR = Path("/home/Lazycat/mysite/templates")
DEFAULT_TEMPLATES_DIR = PROJECT_ROOT / "templates"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class TemplateLoader:
    """Load external email templates dynamically based on user and category"""

    def __init__(self, base_dir: Optional[str] = None, user_id: Optional[str] = None):
        self.user_id = user_id
        if user_id:
            self.base_dir = str(PROJECT_ROOT / "users" / user_id / "templates")
            os.makedirs(self.base_dir, exist_ok=True)
        else:
            self.base_dir = base_dir or str(DEFAULT_TEMPLATES_DIR)
        self.templates_cache: Dict[str, str] = {}

    def _read_file(self, filename: str) -> str:
        path = os.path.join(self.base_dir, filename)
        if not os.path.exists(path):
            # Fall back to global templates directory
            global_path = os.path.join(str(DEFAULT_TEMPLATES_DIR), filename)
            if not os.path.exists(global_path):
                return ""
            path = global_path
            
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()

    def get_template(self, key: str) -> str:
        if key in self.templates_cache:
            return self.templates_cache[key]
        
        filename = f"{key}.txt"
        content = self._read_file(filename)
        
        # Fallback to software_dev.txt or ai_ml.txt if custom one is missing
        if not content:
            if key == "ai":
                content = self._read_file("ai_ml.txt")
            else:
                content = self._read_file("software_dev.txt")
                
        self.templates_cache[key] = content
        return content

class EmailTemplateGenerator:
    """Generate personalized email templates dynamically using category preferences"""

    def __init__(self, candidate_name: str = "Your Name",
                 candidate_email: str = "your.email@example.com",
                 resume_path: str = "",
                 template_loader: Optional[TemplateLoader] = None,
                 preference_manager: Optional[PreferenceManager] = None,
                 user_id: Optional[str] = None):
        self.user_id = user_id
        self.preference_manager = preference_manager or PreferenceManager(user_id=user_id)
        self.candidate_name = candidate_name
        self.candidate_email = candidate_email
        self.resume_path = resume_path
        self.template_loader = template_loader or TemplateLoader(user_id=user_id)

    def generate_email(self, lead: ExtractedLead) -> Dict[str, str]:
        """Generate dynamic personalized subject and body based on job category"""
        category_key = lead.email_template_type or 'software_dev'
        categories = self.preference_manager.categories()
        
        # 1. Resolve custom subject template
        subject_template = "Application for {job_title} at {company_name}"
        if category_key in categories:
            subject_template = categories[category_key].get("email_subject", subject_template)
            
        subject = subject_template.format(
            job_title=lead.job_title or "Software Developer",
            company_name=lead.company_name or "your organization"
        )
        
        # 2. Resolve body template
        raw_body = self.template_loader.get_template(category_key)
        if not raw_body:
            raw_body = "Dear Hiring Manager,\n\nI am writing to apply for the {job_title} position at {company_name}.\n\nBest regards,\n{candidate_name}"
            
        # Format the body
        body = raw_body.format(
            candidate_name=self.candidate_name,
            candidate_email=self.candidate_email,
            job_title=lead.job_title or "Software Developer",
            company_name=lead.company_name or "your organization",
            tech_stack=", ".join(lead.tech_stack[:5]) if lead.tech_stack else "modern technologies",
            skills=", ".join(lead.skills_required[:3]) if lead.skills_required else "innovative solutions"
        )
        
        return {"subject": subject, "body": body}


class ColdEmailSystem:
    """Main system to manage the complete cold email workflow"""

    def __init__(self, extractor: LinkedInLeadExtractor, email_generator: EmailTemplateGenerator,
                 config_manager: ConfigManager = None, user_id: Optional[str] = None):
        self.user_id = user_id
        self.storage_user_id = user_id or "default_user"  # DB rows are always scoped to a user_id
        self.extractor = extractor
        self.email_generator = email_generator
        self.config_manager = config_manager or ConfigManager(user_id=user_id)
        db_store.ensure_user(self.storage_user_id)

    def _can_send_email(self, post_urn: str, to_email: Optional[str]) -> bool:
        return db_store.can_send_email(self.storage_user_id, post_urn, to_email)

    def _log_sent_email(self, lead: ExtractedLead, to_email: str):
        db_store.mark_email_sent(self.storage_user_id, lead.post_urn, lead.email_template_type)

    def process_and_prepare_emails(self, posts_data: List[Dict]) -> Tuple[List[ExtractedLead], Dict]:
        """Complete workflow: filter -> extract -> deduplicate -> prepare emails -> optionally send

        Leads are saved to the DB as they're extracted -- on_lead_extracted below fires per lead,
        right after its own LLM call finishes, instead of waiting for process_posts_batch to
        finish every post in the run. The whole-batch remove_duplicates/save_leads pass further
        down still runs afterwards as an idempotent backstop (in case a lead's callback failed,
        or job filtering/final stats need recomputing) -- it re-saves already-streamed leads too,
        which is harmless since save_leads is INSERT OR IGNORE keyed on (user_id, post_urn).
        """
        logging.info("🔍 Starting LinkedIn Lead Processing...")

        raw_item_id_by_urn = {
            str(item['urn']): item['_raw_item_id']
            for item in posts_data
            if item.get('_raw_item_id') and item.get('urn')
        }

        # Seeds the dedup set once, up front, then mutated in place as leads stream in --
        # streaming_lock guards the whole check-then-add-then-save critical section since
        # process_posts_batch may call this from multiple chunk threads concurrently.
        seen_keys = set(db_store.get_recent_duplicate_keys(self.storage_user_id, days=5))
        streaming_lock = threading.Lock()
        streamed_count = 0

        def _on_lead_extracted(lead: ExtractedLead, post: Dict):
            nonlocal streamed_count
            if not lead.is_job_posting:
                return
            with streaming_lock:
                if not self.extractor.prepare_unique_lead(lead, seen_keys):
                    return
                raw_item_id = raw_item_id_by_urn.get(str(lead.post_urn))
                inserted = db_store.save_leads(
                    self.storage_user_id, [lead],
                    {str(lead.post_urn): raw_item_id} if raw_item_id else {}
                )
                streamed_count += inserted
            raw_item_id_for_post = post.get('_raw_item_id')
            if raw_item_id_for_post:
                db_store.mark_attempted(self.storage_user_id, raw_item_id_for_post, matched=True)

        leads, filter_stats = self.extractor.process_posts_batch(posts_data, on_lead_extracted=_on_lead_extracted)
        logging.info(f"💾 Streamed {streamed_count} lead(s) to database during extraction")
        matched_urns = {lead.post_urn for lead in leads}

        def _mark_all_attempted():
            # Only call this once the outcome is durably saved (or confirmed to be "no match") --
            # marking earlier and then crashing before the save would silently lose the item forever.
            # Idempotent for items _on_lead_extracted already marked (mark_attempted is an upsert).
            for item in posts_data:
                raw_item_id = item.get('_raw_item_id')
                if raw_item_id:
                    db_store.mark_attempted(self.storage_user_id, raw_item_id, matched=item.get('urn') in matched_urns)

        if not leads:
            logging.warning("❌ No leads found matching your preferences!")
            _mark_all_attempted()
            return [], {"error": "No matching leads found"}

        job_leads = self.extractor.filter_job_posts(leads)
        logging.info(f"📋 Found {len(job_leads)} job-related posts")

        recent_duplicate_keys = db_store.get_recent_duplicate_keys(self.storage_user_id, days=5)
        unique_leads, duplicate_stats = self.extractor.remove_duplicates(job_leads, existing_keys=recent_duplicate_keys)
        logging.info(f"\n📊 DEDUPLICATION SUMMARY:")
        logging.info(f"Total Job Posts: {duplicate_stats['total_leads']}")
        logging.info(f"Unique Leads: {duplicate_stats['unique_leads']}")
        logging.info(f"Duplicates Removed: {duplicate_stats['duplicates_removed']} "
                     f"({duplicate_stats['cross_run_duplicates_removed']} matched a lead from the last 5 days)")

        if duplicate_stats['duplicate_details']:
            logging.info(f"\n🔄 Duplicate Entries Found:")
            for dup in duplicate_stats['duplicate_details'][:5]:
                logging.info(f"  - {dup['company']} | {dup['job_title']} | by {dup['author']}")

        # Always prepare email previews and export data
        self.prepare_email_previews(unique_leads)

        # Backstop save -- see docstring. Already-streamed leads no-op here (same post_urn).
        newly_inserted = db_store.save_leads(self.storage_user_id, unique_leads, raw_item_id_by_urn)
        logging.info(f"💾 Saved {newly_inserted} new leads to database ({len(unique_leads)} total this run)")
        _mark_all_attempted()

        # Check auto-email flag
        auto_email_enabled = self.config_manager.is_auto_email_enabled()

        stats = {
            "filtering": filter_stats,
            "deduplication": duplicate_stats,
            "final_leads": len(unique_leads),
            "auto_email_enabled": auto_email_enabled
        }

        if auto_email_enabled:
            logging.info("🚀 Auto-email enabled, proceeding to send emails...")
            email_leads, manual_leads = self.get_sendable_leads(unique_leads)

            if email_leads:
                sent, skipped = self.send_emails_with_cooldown(email_leads)
                stats.update({
                    "emails_sent": sent,
                    "emails_skipped": skipped,
                    "manual_followup_needed": len(manual_leads)
                })
                logging.info(f"📧 Email Summary: {sent} sent, {skipped} skipped, {len(manual_leads)} manual follow-up")
            else:
                logging.info("📧 No email leads found for automatic sending")
                stats.update({
                    "emails_sent": 0,
                    "emails_skipped": 0,
                    "manual_followup_needed": len(unique_leads)
                })
        else:
            logging.info("📧 Auto-email disabled. Leads prepared but emails not sent.")
            logging.info("💡 To enable auto-email, set 'auto_email': true in config.json")
            stats.update({
                "emails_sent": 0,
                "emails_skipped": 0,
                "auto_email_disabled": True
            })

        return unique_leads, stats

    def send_emails_with_cooldown(self, leads: List[ExtractedLead]) -> Tuple[int, int]:
        """Send emails with resume attachments and enforce 7-day cooldown by post_urn or email."""
        sent = 0
        skipped = 0

        # Import email service dynamically, trying user-specific settings first
        try:
            from enhanced_emailservice import EmailService
            sender_email = self.config_manager.get("sender_email")
            sender_password = self.config_manager.get("sender_password")
            smtp_server = self.config_manager.get("smtp_server", "smtp.gmail.com")
            smtp_port = self.config_manager.get("smtp_port", 587)
            
            if sender_email and sender_password:
                email_service = EmailService(
                    sender_email=sender_email,
                    sender_password=sender_password,
                    smtp_server=smtp_server,
                    smtp_port=smtp_port,
                    user_id=self.user_id
                )
            else:
                from enhanced_emailservice import create_email_service_from_env
                email_service = create_email_service_from_env()
                if email_service:
                    email_service.user_id = self.user_id
                    
            if not email_service:
                print("❌ Email service not configured. Set SENDER_EMAIL and SENDER_PASSWORD.")
                return 0, len(leads)
        except ImportError:
            print("❌ Enhanced email service not available. Using simulation mode.")
            email_service = None

        for lead in leads:
            if lead.application_method != 'email' or not lead.application_contact:
                continue
            if not self._can_send_email(lead.post_urn, lead.application_contact):
                skipped += 1
                continue

            if email_service:
                # Generate email content dynamically
                email_content = self.email_generator.generate_email(lead)

                # Send actual email
                success = email_service.send_single_email(
                    to_email=lead.application_contact,
                    subject=email_content['subject'],
                    body=email_content['body'],
                    template_type=lead.email_template_type or 'software_dev'
                )

                if success:
                    self._log_sent_email(lead, lead.application_contact)
                    sent += 1
                else:
                    skipped += 1
            else:
                # Simulation mode
                self._log_sent_email(lead, lead.application_contact)
                sent += 1

        print(f"Emails sent: {sent}, skipped due to cooldown/errors: {skipped}")
        return sent, skipped

    def prepare_email_previews(self, leads: List[ExtractedLead]):
        """Generate and display email previews"""
        template_counts = {}

        print(f"\n📧 EMAIL PREPARATION SUMMARY:")
        print(f"Total Emails to Send: {len(leads)}")

        for lead in leads:
            cat = lead.email_template_type or 'software_dev'
            template_counts[cat] = template_counts.get(cat, 0) + 1

        print(f"Template Distribution:")
        for cat, count in template_counts.items():
            print(f"  - {cat}: {count}")

        print(f"\n{'='*60}")
        print("📬 EMAIL PREVIEWS (First 3 emails):")
        print(f"{'='*60}")

        for i, lead in enumerate(leads[:3]):
            email_content = self.email_generator.generate_email(lead)
            print(f"\n--- EMAIL {i+1} ---")
            print(f"📍 TO: {lead.application_contact or 'Contact method: ' + lead.application_method}")
            print(f"🏢 COMPANY: {lead.company_name or 'Unknown'}")
            print(f"💼 ROLE: {lead.job_title or 'Unknown'}")
            print(f"📝 TEMPLATE: {lead.email_template_type}")
            print(f"🔗 POST URL: {lead.post_url or 'Not available'}")
            print(f"SUBJECT: {email_content['subject']}")
            print("\nBODY:")
            print("-" * 40)
            print(email_content['body'])
            print("-" * 40)

        if len(leads) > 3:
            print(f"\n... and {len(leads) - 3} more emails ready to send!")

    def get_sendable_leads(self, leads: List[ExtractedLead]) -> Tuple[List[ExtractedLead], List[ExtractedLead]]:
        """Separate leads that can be emailed vs need manual follow-up"""
        email_leads = []
        manual_leads = []

        for lead in leads:
            if lead.application_method == 'email' and lead.application_contact:
                email_leads.append(lead)
            else:
                manual_leads.append(lead)

        return email_leads, manual_leads

    def export_to_csv(self, leads: List[ExtractedLead], filename: str) -> pd.DataFrame:
        """Export extracted leads to CSV with proper timestamp and template tracking"""
        os.makedirs(os.path.dirname(filename), exist_ok=True)
        data = [asdict(lead) for lead in leads]
        new_df = pd.DataFrame(data)

        # Add timestamp for tracking
        new_df['created_at'] = datetime.now().isoformat()
        new_df['email_sent'] = False
        new_df['email_sent_at'] = None
        new_df['template_used'] = new_df['email_template_type']

        if os.path.exists(filename):
            try:
                existing_df = pd.read_csv(filename)
                missing_cols = [c for c in new_df.columns if c not in existing_df.columns]
                for c in missing_cols:
                    existing_df[c] = None
                missing_cols2 = [c for c in existing_df.columns if c not in new_df.columns]
                for c in missing_cols2:
                    new_df[c] = None

                if 'post_urn' in existing_df.columns and 'post_urn' in new_df.columns:
                    existing_urns = set(existing_df['post_urn'].astype(str))
                    new_urns = set(new_df['post_urn'].astype(str))
                    truly_new_urns = new_urns - existing_urns
                    if truly_new_urns:
                        truly_new_df = new_df[new_df['post_urn'].astype(str).isin(truly_new_urns)]
                        combined = pd.concat([existing_df, truly_new_df], ignore_index=True)
                        combined.to_csv(filename, index=False)
                        print(f"Appended {len(truly_new_df)} new leads; total {len(combined)} in {filename}")
                        return combined
                    else:
                        print(f"No new leads to append; {len(existing_df)} total in {filename}")
                        return existing_df
                else:
                    combined = pd.concat([existing_df, new_df], ignore_index=True)
                    combined.to_csv(filename, index=False)
                    print(f"Appended {len(new_df)} leads; total {len(combined)} written to {filename}")
                    return combined
            except Exception as e:
                print(f"Error reading existing CSV: {e}, overwriting")
                pass
        new_df.to_csv(filename, index=False)
        print(f"Exported {len(leads)} leads to {filename}")
        return new_df

    def export_to_json(self, leads: List[ExtractedLead], filename: str):
        """Export extracted leads to JSON"""
        os.makedirs(os.path.dirname(filename), exist_ok=True)
        data = [asdict(lead) for lead in leads]
        existing: List[Dict] = []
        if os.path.exists(filename):
            try:
                with open(filename, 'r', encoding='utf-8') as f:
                    existing = json.load(f)
            except Exception:
                existing = []
        
        merged_by_urn = {}
        for item in existing:
            merged_by_urn[str(item.get('post_urn'))] = item
        for item in data:
            merged_by_urn[str(item.get('post_urn'))] = item
        merged_list = list(merged_by_urn.values())
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(merged_list, f, indent=2, ensure_ascii=False)
        print(f"Exported {len(merged_list)} total leads to {filename}")
