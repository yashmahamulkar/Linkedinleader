from typing import Dict, List, Optional, Union, Tuple
import json
import os
import logging
from dotenv import load_dotenv
from datetime import datetime, timedelta
from dataclasses import asdict
from keymanager import KeyManager
from config import ConfigManager
from preferencemanager import PreferenceManager
from extractor import LinkedInLeadExtractor, ExtractedLead
import pandas as pd

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class TemplateLoader:
    """Load external email templates from files.

    Files:
      templates/software_dev.txt
      templates/ai_ml.txt
    """

    def __init__(self, base_dir: str = "/home/Lazycat/mysite/templates"):
        self.base_dir = base_dir
        self.templates_cache: Dict[str, str] = {}

    def _read_file(self, filename: str) -> str:
        path = os.path.join(self.base_dir, filename)
        if not os.path.exists(path):
            return ""
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()

    def get_template(self, key: str) -> str:
        if key in self.templates_cache:
            return self.templates_cache[key]
        filename = {
            'software_dev': 'software_dev.txt',
            'ai': 'ai_ml.txt'
        }.get(key)
        if not filename:
            return ""
        content = self._read_file(filename)
        self.templates_cache[key] = content
        return content

class EmailTemplateGenerator:
    """Generate personalized email templates based on job type"""

    def __init__(self, candidate_name: str = "Your Name",
                 candidate_email: str = "your.email@example.com",
                 resume_path: str = "path/to/your/resume.pdf",
                 template_loader: Optional[TemplateLoader] = None):
        self.candidate_name = candidate_name
        self.candidate_email = candidate_email
        self.resume_path = resume_path
        self.template_loader = template_loader or TemplateLoader()

    def get_software_dev_template(self, lead: ExtractedLead) -> Dict[str, str]:
        """Email template for Software Developer roles (externalized)."""
        subject = f"Application for {lead.job_title} at {lead.company_name or 'Your Company'}"
        raw = self.template_loader.get_template('software_dev')
        body = (raw or "").format(
            candidate_name=self.candidate_name,
            candidate_email=self.candidate_email,
            job_title=lead.job_title or "Software Developer",
            company_name=lead.company_name or "your organization",
            tech_stack=", ".join(lead.tech_stack[:5]) if lead.tech_stack else "modern technologies",
            skills=", ".join(lead.skills_required[:3]) if lead.skills_required else "innovative solutions"
        )
        return {"subject": subject, "body": body}

    # Removed DevOps template: only software_dev and ai are supported now

    def get_ai_template(self, lead: ExtractedLead) -> Dict[str, str]:
        """Email template for AI/ML/Data Science roles (externalized)."""
        subject = f"AI/ML Role Application - {lead.job_title} at {lead.company_name or 'Your Company'}"
        raw = self.template_loader.get_template('ai')
        body = (raw or "").format(
            candidate_name=self.candidate_name,
            candidate_email=self.candidate_email,
            job_title=lead.job_title or "AI/ML Engineer",
            company_name=lead.company_name or "your organization",
            tech_stack=", ".join(lead.tech_stack[:5]) if lead.tech_stack else "AI/ML technologies",
            skills=", ".join(lead.skills_required[:3]) if lead.skills_required else "AI initiatives"
        )
        return {"subject": subject, "body": body}

    # Removed general template to reduce scope

    def generate_email(self, lead: ExtractedLead) -> Dict[str, str]:
        """Generate appropriate email based on job type"""
        template_type = lead.email_template_type or 'software_dev'
        if template_type == 'software_dev':
            return self.get_software_dev_template(lead)
        elif template_type == 'ai':
            return self.get_ai_template(lead)
        return self.get_software_dev_template(lead)


class ColdEmailSystem:
    """Main system to manage the complete cold email workflow"""

    def __init__(self, extractor: LinkedInLeadExtractor, email_generator: EmailTemplateGenerator,
                 config_manager: ConfigManager = None):
        self.extractor = extractor
        self.email_generator = email_generator
        self.config_manager = config_manager or ConfigManager()
        self.sent_log_file = 'emails_sent.csv'

    def _load_sent_log(self) -> pd.DataFrame:
        if os.path.exists(self.sent_log_file):
            try:
                return pd.read_csv(self.sent_log_file)
            except Exception:
                return pd.DataFrame(columns=['post_urn', 'company_name', 'job_title', 'to_email', 'template_type', 'sent_at'])
        return pd.DataFrame(columns=['post_urn', 'company_name', 'job_title', 'to_email', 'template_type', 'sent_at'])

    def _save_sent_log(self, df: pd.DataFrame):
        df.to_csv(self.sent_log_file, index=False)

    def _can_send_email(self, post_urn: str, to_email: Optional[str]) -> bool:
        if not to_email:
            return False
        df = self._load_sent_log()
        if df.empty:
            return True
        mask = (df['post_urn'] == post_urn) | (df['to_email'] == to_email)
        recent = df[mask]
        if recent.empty:
            return True
        try:
            recent['sent_at'] = pd.to_datetime(recent['sent_at'], errors='coerce')
        except Exception:
            return True
        last_time = recent['sent_at'].max()
        if pd.isna(last_time):
            return True
        return datetime.now() - last_time.to_pydatetime() >= timedelta(days=7)

    def _log_sent_email(self, lead: ExtractedLead, to_email: str):
        df = self._load_sent_log()
        new_row = {
            'post_urn': lead.post_urn,
            'company_name': lead.company_name,
            'job_title': lead.job_title,
            'to_email': to_email,
            'template_type': lead.email_template_type,
            'sent_at': datetime.now().isoformat()
        }
        df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
        self._save_sent_log(df)

    def process_and_prepare_emails(self, posts_data: List[Dict]) -> Tuple[List[ExtractedLead], Dict]:
        """Complete workflow: filter -> extract -> deduplicate -> prepare emails -> optionally send"""
        logging.info("🔍 Starting LinkedIn Lead Processing...")

        leads, filter_stats = self.extractor.process_posts_batch(posts_data)
        if not leads:
            logging.warning("❌ No leads found matching your preferences!")
            return [], {"error": "No matching leads found"}

        job_leads = self.extractor.filter_job_posts(leads)
        logging.info(f"📋 Found {len(job_leads)} job-related posts")

        unique_leads, duplicate_stats = self.extractor.remove_duplicates(job_leads)
        logging.info(f"\n📊 DEDUPLICATION SUMMARY:")
        logging.info(f"Total Job Posts: {duplicate_stats['total_leads']}")
        logging.info(f"Unique Leads: {duplicate_stats['unique_leads']}")
        logging.info(f"Duplicates Removed: {duplicate_stats['duplicates_removed']}")

        if duplicate_stats['duplicate_details']:
            logging.info(f"\n🔄 Duplicate Entries Found:")
            for dup in duplicate_stats['duplicate_details'][:5]:
                logging.info(f"  - {dup['company']} | {dup['job_title']} | by {dup['author']}")

        # Always prepare email previews and export data
        self.prepare_email_previews(unique_leads)

        # Export leads regardless of auto-email setting
        self.export_to_csv(unique_leads, 'unique_filtered_leads.csv')
        self.export_to_json(unique_leads, 'unique_filtered_leads.json')

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

        # Import email service
        try:
            from enhanced_emailservice import create_email_service_from_env
            email_service = create_email_service_from_env()
            if not email_service:
                print("❌ Email service not configured. Set SENDER_EMAIL and SENDER_PASSWORD environment variables.")
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
                # Generate email content
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
        template_counts = {'software_dev': 0, 'ai': 0}

        print(f"\n📧 EMAIL PREPARATION SUMMARY:")
        print(f"Total Emails to Send: {len(leads)}")

        for lead in leads:
            template_counts[lead.email_template_type] += 1

        print(f"Template Distribution:")
        print(f"  🖥️  Software Developer: {template_counts['software_dev']}")
        print(f"  🤖 AI/ML/Data Science: {template_counts['ai']}")

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

    def export_to_csv(self, leads: List[ExtractedLead], filename: str = 'extracted_leads.csv') -> pd.DataFrame:
        """Export extracted leads to CSV with proper timestamp and template tracking"""
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
                # Ensure same columns
                missing_cols = [c for c in new_df.columns if c not in existing_df.columns]
                for c in missing_cols:
                    existing_df[c] = None
                missing_cols2 = [c for c in existing_df.columns if c not in new_df.columns]
                for c in missing_cols2:
                    new_df[c] = None

                # Only append truly new entries (not already in existing)
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
                    # Fallback: append all if no post_urn column
                    combined = pd.concat([existing_df, new_df], ignore_index=True)
                    combined.to_csv(filename, index=False)
                    print(f"Appended {len(new_df)} leads; total {len(combined)} written to {filename}")
                    return combined
            except Exception as e:
                print(f"Error reading existing CSV: {e}, overwriting")
                # Fallback to overwrite if read fails
                pass
        new_df.to_csv(filename, index=False)
        print(f"Exported {len(leads)} leads to {filename}")
        return new_df

    def export_to_json(self, leads: List[ExtractedLead], filename: str = 'extracted_leads.json'):
        """Export extracted leads to JSON"""
        data = [asdict(lead) for lead in leads]
        existing: List[Dict] = []
        if os.path.exists(filename):
            try:
                with open(filename, 'r', encoding='utf-8') as f:
                    existing = json.load(f)
            except Exception:
                existing = []
        # Merge by post_urn uniqueness
        merged_by_urn = {}
        for item in existing:
            merged_by_urn[str(item.get('post_urn'))] = item
        for item in data:
            merged_by_urn[str(item.get('post_urn'))] = item
        merged_list = list(merged_by_urn.values())
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(merged_list, f, indent=2, ensure_ascii=False)
        print(f"Exported {len(merged_list)} total leads to {filename}")
