import sys
import io

# Force UTF-8 stdout/stderr. Without this, any print()/logging call containing an emoji
# (there are many across script.py/emailmanager.py) raises UnicodeEncodeError as soon as
# stdout isn't an interactive console -- e.g. inside a background thread whose output is
# redirected -- silently aborting extraction mid-pipeline before leads ever get saved.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from flask import Flask, request, jsonify
from flask_cors import CORS
import pandas as pd
import numpy as np
import os
import json
import re
import logging
import threading
from dataclasses import asdict
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any, List

# DB module is required by auth and most API routes; import it independently so
# it remains available even if an optional module import below fails.
import db as db_store

# Import email and extraction components
try:
    from enhanced_emailservice import EmailService, create_email_service_from_env
    from emailmanager import TemplateLoader, EmailTemplateGenerator, ColdEmailSystem
    from config import ConfigManager
    from preferencemanager import PreferenceManager
    from extractor import LinkedInLeadExtractor, ExtractedLead
    from script import LinkedInRunner, create_default_search_urls, create_default_job_search_input, strip_html
except ImportError as e:
    print(f"Warning: Could not import core modules: {e}")

app = Flask(__name__)

# Enable CORS for any origin on port 9002 with credentials support (supports dynamic IPs/interfaces)
CORS(app, resources={r"/*": {"origins": [r"http://.*:9002", r"https://.*:9002"]}}, supports_credentials=True)

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize DB at process startup so leadflow.db and tables exist before first request.
# init_db() is idempotent, so this is safe even when the DB already exists.
try:
    if 'db_store' in globals() and db_store is not None:
        db_store.init_db()
        logger.info("Database initialized at startup")
except Exception as e:
    logger.error(f"Database startup initialization failed: {e}")

# Real status of the last/current background scrape pipeline run, polled by the frontend
# instead of a fabricated progress bar. Guarded by a lock since the background thread
# writes it while request handlers read it concurrently.
_pipeline_status_lock = threading.Lock()
pipeline_status: Dict[str, Any] = {
    "running": False,
    "stage": "idle",  # idle | scraping | extracting | done | error
    "started_at": None,
    "finished_at": None,
    "raw_items_scraped": 0,
    "raw_items_new": 0,
    "users_total": 0,
    "users_processed": 0,
    "current_user": None,
    "error": None,
    "metrics": {},
}


def _update_pipeline_status(**kwargs):
    with _pipeline_status_lock:
        pipeline_status.update(kwargs)


# Path configuration
PROJECT_ROOT = Path(__file__).resolve().parent
LINKEDIN_JSON_PATH = str(PROJECT_ROOT / 'linkedin_data.json')
LINKEDIN_JOBS_PATH = str(PROJECT_ROOT / 'linkedin_jobs.json')
USERS_DIR = PROJECT_ROOT / 'users'

def clean_nan_values(obj):
    """Recursively replace NaN values with None (which becomes null in JSON)"""
    if isinstance(obj, dict):
        return {k: clean_nan_values(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [clean_nan_values(item) for item in obj]
    elif pd.isna(obj) or (isinstance(obj, float) and np.isnan(obj)):
        return None
    else:
        return obj

def _raw_item_display_text(raw_item: Dict[str, Any]) -> str:
    """Best-effort plain-text description across the raw shapes this app stores
    (LinkedIn posts, LinkedIn jobs, Indeed jobs, Glassdoor jobs) -- Indeed's 'description' is a
    nested {text, html} dict rather than a plain string, and Glassdoor's is an HTML fragment."""
    text = raw_item.get('text') or raw_item.get('jobDescription')
    if text:
        return text
    description = raw_item.get('description')
    if isinstance(description, dict):
        return strip_html(description.get('html')) if not description.get('text') else description['text']
    return strip_html(description) if description else ''

def _raw_item_display_location(raw_item: Dict[str, Any]) -> str:
    """Best-effort plain-text location across the raw shapes -- Indeed's 'location' is a nested
    address-component dict and Glassdoor's is a {id, name, type} dict, not plain strings."""
    location = raw_item.get('authorHeadline') or raw_item.get('location')
    if isinstance(location, dict):
        if location.get('name'):  # Glassdoor: already a formatted "City, ST" string
            return location['name']
        parts = [location.get('city'), location.get('admin3Code'), location.get('admin1Code'), location.get('countryName')]
        return ", ".join(dict.fromkeys(p for p in parts if p))
    return location or ''

def get_user_components(user_id: str) -> Dict[str, Any]:
    """Helper to dynamically resolve and load user-specific configuration and services"""
    user_dir = USERS_DIR / user_id
    os.makedirs(user_dir, exist_ok=True)
    os.makedirs(user_dir / "resumes", exist_ok=True)
    os.makedirs(user_dir / "templates", exist_ok=True)
    os.makedirs(user_dir / "configs", exist_ok=True)

    cfg = ConfigManager(user_id=user_id)
    pref = PreferenceManager(user_id=user_id)
    loader = TemplateLoader(user_id=user_id)
    
    generator = EmailTemplateGenerator(
        candidate_name=cfg.get("candidate_name", "John Doe"),
        candidate_email=cfg.get("candidate_email", "john.doe@email.com"),
        resume_path=cfg.get("resume_path", ""),
        template_loader=loader,
        preference_manager=pref,
        user_id=user_id
    )
    
    # Resolve custom SMTP credentials
    sender_email = cfg.get("sender_email")
    sender_password = cfg.get("sender_password")
    smtp_server = cfg.get("smtp_server", "smtp.gmail.com")
    smtp_port = cfg.get("smtp_port", 587)
    
    if sender_email and sender_password:
        srv = EmailService(
            sender_email=sender_email,
            sender_password=sender_password,
            smtp_server=smtp_server,
            smtp_port=smtp_port,
            user_id=user_id
        )
    else:
        srv = create_email_service_from_env()
        if srv:
            srv.user_id = user_id
            
    return {
        "config_manager": cfg,
        "preference_manager": pref,
        "template_loader": loader,
        "email_generator": generator,
        "email_service": srv,
        "email_enabled": srv is not None
    }

# ----------------- BASE SYSTEM ROUTES -----------------

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({
        "status": "ok",
        "global_linkedin_data_exists": os.path.exists(LINKEDIN_JSON_PATH)
    }), 200

@app.route('/api/raw-items', methods=['GET'])
def get_raw_items():
    """Recent raw scraped items (global, not per-user) -- powers the Scraped Data page."""
    try:
        source = request.args.get('source')
        limit = int(request.args.get('limit', 50))
        items = db_store.get_recent_raw_items(source=source, limit=limit)
        return jsonify(clean_nan_values({"success": True, "items": items})), 200
    except Exception as e:
        logger.error(f"Error fetching raw items: {e}")
        return jsonify({"error": str(e)}), 500

# ----------------- BACKWARD-COMPATIBLE ROOT ROUTES (MAP TO default_user) -----------------

@app.route('/leads', methods=['GET'])
def root_leads():
    return get_user_leads("default_user")

@app.route('/leads-emails', methods=['GET'])
def root_leads_emails():
    return get_user_email_leads("default_user")

@app.route('/leads-manual', methods=['GET'])
def root_leads_manual():
    return get_user_manual_leads("default_user")

@app.route('/dashboard/summary', methods=['GET'])
def root_dashboard_summary():
    return get_user_dashboard_summary("default_user")

@app.route('/send-email', methods=['POST'])
def root_send_email():
    return send_user_email("default_user")

@app.route('/send-custom-email', methods=['POST'])
def root_send_custom_email():
    return send_user_custom_email("default_user")

@app.route('/target-companies', methods=['GET'])
def root_get_target_companies():
    return get_user_target_companies("default_user")

@app.route('/target-companies', methods=['POST'])
def root_add_target_company():
    return add_user_target_company("default_user")

@app.route('/target-companies/<path:company_slug>', methods=['DELETE'])
def root_remove_target_company(company_slug: str):
    return remove_user_target_company("default_user", company_slug)

@app.route('/preferences', methods=['GET'])
def root_get_preferences():
    return get_user_preferences("default_user")

@app.route('/preferences', methods=['POST'])
def root_save_preferences():
    return save_user_preferences("default_user")

@app.route('/categories', methods=['GET'])
def root_get_categories():
    return get_user_categories("default_user")

@app.route('/categories', methods=['POST'])
def root_save_categories():
    return save_user_categories("default_user")

@app.route('/resume/<category>', methods=['POST'])
def root_upload_resume(category):
    return upload_user_resume("default_user", category)

@app.route('/template/<category>', methods=['GET'])
def root_get_template(category):
    return get_user_template("default_user", category)

@app.route('/template/<category>', methods=['POST'])
def root_upload_template(category):
    return upload_user_template("default_user", category)

# ----------------- MULTI-USER AUTHENTICATION -----------------

@app.route('/api/auth/login', methods=['POST'])
def auth_login():
    try:
        import re
        import json
        import shutil
        data = request.get_json() or {}
        raw_user_id = data.get("userId", "")
        raw_password = data.get("password", "")

        # Guard against null/non-string payload fields to avoid 500s from .strip()
        user_id = raw_user_id.strip().lower() if isinstance(raw_user_id, str) else ""
        user_id = re.sub(r'[^a-z0-9_]', '', user_id)
        password = raw_password.strip() if isinstance(raw_password, str) else ""
        
        if not user_id:
            return jsonify({"success": False, "message": "User ID is required"}), 400
        if not password:
            return jsonify({"success": False, "message": "Password is required"}), 400

        # Load hardcoded credentials from users_credentials.json
        credentials = {}
        credentials_paths = [
            PROJECT_ROOT / "users_credentials.json",           # legacy location
            PROJECT_ROOT / "configs" / "users_credentials.json",  # current config folder
        ]
        for credentials_file in credentials_paths:
            if os.path.exists(credentials_file):
                try:
                    with open(credentials_file, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
                    if isinstance(loaded, dict):
                        credentials = loaded
                        break
                    logger.warning(f"Ignoring credentials file with invalid format (expected object): {credentials_file}")
                except Exception as e:
                    logger.error(f"Failed to read credentials file {credentials_file}: {e}")
        
        # Fallback if empty or not found
        if not credentials:
            credentials = {
                "yash": "Lazycat@2004",
                "rucha": "Lazycat@2003"
            }

        if user_id not in credentials:
            return jsonify({"success": False, "message": "Incorrect User ID or Password"}), 401
            
        if credentials[user_id] != password:
            return jsonify({"success": False, "message": "Incorrect User ID or Password"}), 401

        # Successful authentication! Dynamically provision sandbox if not exists
        user_dir = USERS_DIR / user_id
        is_new_sandbox = not os.path.exists(user_dir) or not os.listdir(user_dir)

        comps = get_user_components(user_id)
        cfg = comps["config_manager"]
        db_store.ensure_user(user_id, name=cfg.get("candidate_name"), email=cfg.get("candidate_email"))

        # If new sandbox, copy global templates and initialize config
        if is_new_sandbox:
            cfg.set("candidate_name", user_id.title())

            # Copy global email templates
            global_templates_dir = PROJECT_ROOT / "templates"
            user_templates_dir = user_dir / "templates"
            if os.path.exists(global_templates_dir):
                for t_file in os.listdir(global_templates_dir):
                    if t_file.endswith(".txt") and os.path.isfile(global_templates_dir / t_file):
                        shutil.copy(str(global_templates_dir / t_file), str(user_templates_dir / t_file))

        return jsonify({
            "success": True,
            "userId": user_id,
            "name": cfg.get("candidate_name", user_id.title()),
            "email": cfg.get("candidate_email", "")
        }), 200
    except Exception as e:
        logger.exception("Login error")
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/auth/register', methods=['POST'])
def auth_register():
    return jsonify({"success": False, "message": "Self-registration is disabled. Please contact your system administrator."}), 403

# ----------------- USER PROFILE & PREFERENCES -----------------

@app.route('/api/users/<user_id>/preferences', methods=['GET'])
def get_user_preferences(user_id: str):
    try:
        comps = get_user_components(user_id)
        cfg = comps["config_manager"]
        pref = comps["preference_manager"]
        
        return jsonify({
            "success": True,
            "preferences": {
                "preferred_roles": pref.preferred_roles(),
                "preferred_locations": pref.preferred_locations(),
                "custom_instructions": pref.custom_instructions(),
                "candidate_name": cfg.get("candidate_name", "Your Name"),
                "candidate_email": cfg.get("candidate_email", "your.email@example.com"),
                "auto_email": cfg.get("auto_email", False),
                "smtp_server": cfg.get("smtp_server", "smtp.gmail.com"),
                "smtp_port": cfg.get("smtp_port", 587),
                "sender_email": cfg.get("sender_email", "")
            }
        }), 200
    except Exception as e:
        logger.error(f"Error fetching preferences for user {user_id}: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/users/<user_id>/preferences', methods=['POST'])
def save_user_preferences(user_id: str):
    try:
        data = request.get_json() or {}
        comps = get_user_components(user_id)
        cfg = comps["config_manager"]
        pref = comps["preference_manager"]
        
        # 1. Update config parameters
        if "candidate_name" in data:
            cfg.set("candidate_name", data["candidate_name"])
        if "candidate_email" in data:
            cfg.set("candidate_email", data["candidate_email"])
        if "auto_email" in data:
            cfg.set("auto_email", bool(data["auto_email"]))
        if "smtp_server" in data:
            cfg.set("smtp_server", data["smtp_server"])
        if "smtp_port" in data:
            cfg.set("smtp_port", int(data["smtp_port"]))
        if "sender_email" in data:
            cfg.set("sender_email", data["sender_email"])
        if "sender_password" in data:
            cfg.set("sender_password", data["sender_password"])
            
        # 2. Update preference parameters
        if "preferred_roles" in data:
            pref._data["preferred_roles"] = data["preferred_roles"]
        if "preferred_locations" in data:
            pref._data["preferred_locations"] = data["preferred_locations"]
        if "custom_instructions" in data:
            pref._data["custom_instructions"] = data["custom_instructions"]

        pref.save_preferences()
        
        return jsonify({"success": True, "message": "Preferences saved successfully"}), 200
    except Exception as e:
        logger.error(f"Error saving preferences for user {user_id}: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/users/<user_id>/categories', methods=['GET'])
def get_user_categories(user_id: str):
    try:
        comps = get_user_components(user_id)
        pref = comps["preference_manager"]
        return jsonify({
            "success": True,
            "categories": pref.categories()
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/users/<user_id>/categories', methods=['POST'])
def save_user_categories(user_id: str):
    try:
        data = request.get_json() or {}
        if "categories" not in data:
            return jsonify({"error": "Missing categories list in payload"}), 400
            
        comps = get_user_components(user_id)
        pref = comps["preference_manager"]
        pref._data["categories"] = data["categories"]
        pref.save_preferences()
        
        return jsonify({"success": True, "message": "Custom categories updated successfully"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ----------------- RESUME & TEMPLATE UPLOADS -----------------

@app.route('/api/users/<user_id>/resume/<category>', methods=['POST'])
def upload_user_resume(user_id: str, category: str):
    try:
        if 'file' not in request.files:
            return jsonify({"error": "No file part in request"}), 400
            
        file = request.files['file']
        if file.filename == '':
            return jsonify({"error": "No selected file"}), 400
            
        if not file.filename.lower().endswith('.pdf'):
            return jsonify({"error": "Only PDF files are allowed"}), 400
            
        # Target path: users/<user_id>/resumes/<category>_resume.pdf
        user_dir = USERS_DIR / user_id
        resumes_dir = user_dir / "resumes"
        os.makedirs(resumes_dir, exist_ok=True)
        
        target_path = resumes_dir / f"{category}_resume.pdf"
        file.save(str(target_path))
        
        logger.info(f"Uploaded resume for user {user_id}, category {category} to {target_path}")
        return jsonify({
            "success": True,
            "message": f"Resume uploaded successfully for category {category}",
            "filename": f"{category}_resume.pdf"
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/users/<user_id>/template/<category>', methods=['POST'])
def upload_user_template(user_id: str, category: str):
    try:
        data = request.get_json() or {}
        template_text = data.get("template_text")
        subject_template = data.get("email_subject")
        
        if not template_text:
            return jsonify({"error": "template_text is required"}), 400
            
        user_dir = USERS_DIR / user_id
        templates_dir = user_dir / "templates"
        os.makedirs(templates_dir, exist_ok=True)
        
        # 1. Save text template file: <category>.txt
        target_path = templates_dir / f"{category}.txt"
        with open(target_path, 'w', encoding='utf-8') as f:
            f.write(template_text)
            
        # 2. Save subject template inside preferences
        comps = get_user_components(user_id)
        pref = comps["preference_manager"]
        categories = pref.categories()
        
        if category not in categories:
            categories[category] = {
                "display_name": category.replace("_", " ").title(),
                "rules": f"Matching keyword rules for {category}"
            }
            
        if subject_template:
            categories[category]["email_subject"] = subject_template
            
        pref._data["categories"] = categories
        pref.save_preferences()
        
        logger.info(f"Saved custom template for user {user_id}, category {category} to {target_path}")
        return jsonify({
            "success": True,
            "message": f"Cover letter template and subject updated successfully for category {category}"
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/users/<user_id>/template/<category>', methods=['GET'])
def get_user_template(user_id: str, category: str):
    try:
        user_dir = USERS_DIR / user_id
        templates_dir = user_dir / "templates"
        target_path = templates_dir / f"{category}.txt"
        
        template_text = ""
        if os.path.exists(target_path):
            with open(target_path, 'r', encoding='utf-8') as f:
                template_text = f.read()
        else:
            # Fall back to global templates folder
            global_path = PROJECT_ROOT / "templates" / f"{category}.txt"
            if os.path.exists(global_path):
                with open(global_path, 'r', encoding='utf-8') as f:
                    template_text = f.read()
            else:
                # Sense defaults
                if category == 'ai':
                    template_text = "Hi,\n\nI am writing to express my interest in the AI/ML Engineer role. With my background in machine learning and generative AI, I'd love to connect.\n\nBest,\n{candidate_name}"
                else:
                    template_text = "Hi,\n\nI am interested in the Software Developer role. Please see my attached resume. I have strong experience in software engineering and web development.\n\nBest,\n{candidate_name}"
        
        comps = get_user_components(user_id)
        pref = comps["preference_manager"]
        categories = pref.categories()
        email_subject = categories.get(category, {}).get("email_subject", "Application for {job_title} at {company_name}")
        
        return jsonify({
            "success": True,
            "category": category,
            "template_text": template_text,
            "email_subject": email_subject
        }), 200
    except Exception as e:
        logger.error(f"Error fetching template for category {category}: {e}")
        return jsonify({"error": str(e)}), 500

# ----------------- LEADS & PIPELINE EXECUTION Scoped by User -----------------

@app.route('/api/users/<user_id>/leads', methods=['GET'])
def get_user_leads(user_id: str):
    try:
        leads_data = db_store.get_leads_for_user(user_id)

        if not leads_data:
            return jsonify({
                "success": True,
                "statistics": {
                    "total_leads": 0,
                    "email_leads": 0,
                    "link_leads": 0,
                    "other_leads": 0,
                    "emails_sent": 0,
                    "internships": 0,
                    "fresher_roles": 0
                },
                "leads": [],
                "raw_csv_data": []
            }), 200

        enhanced_leads = []
        for lead in leads_data:
            urn = lead.get('post_urn')
            raw_item = db_store.get_raw_item_by_urn(urn) or {}
            linkedin_post = {
                "text": _raw_item_display_text(raw_item),
                "title": raw_item.get('title') or raw_item.get('jobTitle') or '',
                "url": raw_item.get('url') or raw_item.get('jobUrl') or '',
                "postedAtISO": (
                    raw_item.get('postedAtISO') or raw_item.get('postedAt')
                    or raw_item.get('datePublished') or raw_item.get('dateOnIndeed') or ''
                ),
                "authorHeadline": _raw_item_display_location(raw_item),
            }

            enhanced_lead = {
                "csv_data": lead,
                "linkedin_post_data": {
                    "text": linkedin_post.get('text', ''),
                    "title": linkedin_post.get('title', ''),
                    "url": linkedin_post.get('url', ''),
                    "posted_at": linkedin_post.get('postedAtISO', ''),
                    "author_headline": linkedin_post.get('authorHeadline', ''),
                },
                "urn": urn,
                "source": lead.get('source'),
                "job_info": {
                    "title": lead.get('job_title'),
                    "company": lead.get('company_name'),
                    "company_logo": lead.get('company_logo'),
                    "location": lead.get('location'),
                    "work_mode": lead.get('work_mode'),
                    "experience_level": lead.get('experience_level'),
                    "salary_range": lead.get('salary_range'),
                    # The "Stipend" column/field shows salary for the (mostly full-time) general
                    # case, and only prefers stipend when the lead is actually an internship --
                    # falling back to whichever of the two is actually populated either way.
                    "stipend_range": (
                        (lead.get('stipend_range') or lead.get('salary_range'))
                        if lead.get('is_internship')
                        else (lead.get('salary_range') or lead.get('stipend_range'))
                    )
                },
                "contact_info": {
                    "application_method": lead.get('application_method'),
                    "application_contact": lead.get('application_contact'),
                    "post_url": lead.get('post_url')
                },
                "author_info": {
                    "name": lead.get('author_name'),
                    "profile": lead.get('author_profile')
                },
                "technical_info": {
                    "tech_stack": lead.get('tech_stack'),
                    "skills_required": lead.get('skills_required'),
                    "email_template_type": lead.get('email_template_type')
                },
                "status_info": {
                    "is_internship": lead.get('is_internship'),
                    "is_fresher": lead.get('is_fresher'),
                    "email_sent": lead.get('email_sent'),
                    "email_sent_at": lead.get('email_sent_at'),
                    "template_used": lead.get('template_used')
                },
                "posting_info": {
                    "posting_date": lead.get('posting_date'),
                    "created_at": lead.get('created_at'),
                    "application_deadline": lead.get('application_deadline'),
                    "eligibility_criteria": lead.get('eligibility_criteria')
                }
            }
            enhanced_leads.append(enhanced_lead)
            
        stats = {
            "total_leads": len(leads_data),
            "email_leads": len([l for l in leads_data if l.get('application_method') == 'email']),
            "link_leads": len([l for l in leads_data if l.get('application_method') == 'link']),
            "other_leads": len([l for l in leads_data if l.get('application_method') == 'other']),
            "emails_sent": len([l for l in leads_data if l.get('email_sent')]),
            "internships": len([l for l in leads_data if l.get('is_internship')]),
            "fresher_roles": len([l for l in leads_data if l.get('is_fresher')])
        }
        
        response_data = {
            "success": True,
            "statistics": stats,
            "leads": enhanced_leads,
            "raw_csv_data": leads_data
        }
        
        return jsonify(clean_nan_values(response_data)), 200
    except Exception as e:
        logger.error(f"Error in get_user_leads: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/users/<user_id>/leads-emails', methods=['GET'])
def get_user_email_leads(user_id: str):
    try:
        res, status = get_user_leads(user_id)
        if status != 200:
            return res, status
        data = res.get_json()
        # Filter email leads
        data["leads"] = [l for l in data["leads"] if l["contact_info"]["application_method"] == "email"]
        data["raw_csv_data"] = [l for l in data["raw_csv_data"] if l.get("application_method") == "email"]
        return jsonify(data), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/users/<user_id>/leads-manual', methods=['GET'])
def get_user_manual_leads(user_id: str):
    try:
        res, status = get_user_leads(user_id)
        if status != 200:
            return res, status
        data = res.get_json()
        data["leads"] = [l for l in data["leads"] if l["contact_info"]["application_method"] == "link"]
        data["raw_csv_data"] = [l for l in data["raw_csv_data"] if l.get("application_method") == "link"]
        # reverse chronological ordering for Link Leads
        data["leads"] = data["leads"][::-1]
        return jsonify(data), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/users/<user_id>/send-email', methods=['POST'])
def send_user_email(user_id: str):
    try:
        data = request.get_json()
        if not data or 'urn' not in data:
            return jsonify({"error": "URN is required in request body"}), 400
            
        urn = data['urn']
        logger.info(f"Processing manual email request for user {user_id}, URN: {urn}")
        
        comps = get_user_components(user_id)

        lead_row = db_store.get_lead_by_urn(user_id, urn)
        if not lead_row:
            return jsonify({"error": f"No lead found with URN: {urn}"}), 404

        # Check if already sent
        if lead_row.get('email_sent'):
            return jsonify({
                "error": "Email already sent for this URN",
                "urn": urn,
                "email_sent_at": lead_row.get('email_sent_at', 'Unknown')
            }), 409

        if lead_row.get('application_method') != 'email' or not lead_row.get('application_contact'):
            return jsonify({"error": "Lead does not have email contact info"}), 400

        if not comps["email_enabled"]:
            return jsonify({"error": "Email service not configured. Setup sender SMTP credentials."}), 503

        # Reconstruct ExtractedLead from the stored row
        lead = ExtractedLead(
            post_urn=lead_row['post_urn'],
            application_method=lead_row['application_method'],
            posting_date=lead_row.get('posting_date'),
            author_name=lead_row.get('author_name'),
            author_profile=lead_row.get('author_profile'),
            is_job_posting=lead_row.get('is_job_posting', True),
            post_category=lead_row.get('post_category'),
            job_title=lead_row.get('job_title'),
            company_name=lead_row.get('company_name'),
            location=lead_row.get('location'),
            work_mode=lead_row.get('work_mode'),
            experience_level=lead_row.get('experience_level'),
            salary_range=lead_row.get('salary_range'),
            tech_stack=lead_row.get('tech_stack') or [],
            skills_required=lead_row.get('skills_required') or [],
            application_contact=lead_row.get('application_contact'),
            post_url=lead_row.get('post_url'),
            duplicate_key=lead_row.get('duplicate_key'),
            email_template_type=lead_row.get('email_template_type')
        )

        # Generate dynamic content
        email_content = comps["email_generator"].generate_email(lead)

        success = comps["email_service"].send_single_email(
            to_email=lead.application_contact,
            subject=email_content['subject'],
            body=email_content['body'],
            template_type=lead.email_template_type or 'software_dev'
        )

        if success:
            db_store.mark_email_sent(user_id, urn, lead.email_template_type)
            return jsonify({
                "success": True,
                "message": "Email sent successfully",
                "urn": urn,
                "email": lead.application_contact
            }), 200
        else:
            return jsonify({"error": "SMTP delivery failed"}), 500
    except Exception as e:
        logger.error(f"Error manual sending: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/users/<user_id>/send-custom-email', methods=['POST'])
def send_user_custom_email(user_id: str):
    """Send a fully custom subject/body for a lead -- used by the Campaigns email composer,
    where the user has hand-edited or AI-drafted the content and wants to send that exact
    text instead of the standard template send_user_email() generates."""
    try:
        data = request.get_json() or {}
        urn = data.get('urn')
        subject = data.get('subject')
        body = data.get('body')
        if not urn or not subject or not body:
            return jsonify({"error": "urn, subject, and body are all required"}), 400

        comps = get_user_components(user_id)

        lead_row = db_store.get_lead_by_urn(user_id, urn)
        if not lead_row:
            return jsonify({"error": f"No lead found with URN: {urn}"}), 404

        if lead_row.get('email_sent'):
            return jsonify({
                "error": "Email already sent for this URN",
                "urn": urn,
                "email_sent_at": lead_row.get('email_sent_at', 'Unknown')
            }), 409

        to_email = lead_row.get('application_contact')
        if lead_row.get('application_method') != 'email' or not to_email:
            return jsonify({"error": "Lead does not have email contact info"}), 400

        if not comps["email_enabled"]:
            return jsonify({"error": "Email service not configured. Setup sender SMTP credentials."}), 503

        if not db_store.can_send_email(user_id, urn, to_email):
            return jsonify({"error": "This contact was emailed recently (7-day cooldown)."}), 409

        success = comps["email_service"].send_single_email(
            to_email=to_email,
            subject=subject,
            body=body,
            template_type=lead_row.get('email_template_type') or 'software_dev'
        )

        if success:
            db_store.mark_email_sent(user_id, urn, lead_row.get('email_template_type'))
            return jsonify({"success": True, "message": "Email sent successfully", "urn": urn, "email": to_email}), 200
        else:
            return jsonify({"error": "SMTP delivery failed"}), 500
    except Exception as e:
        logger.error(f"Error sending custom email: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/users/<user_id>/target-companies', methods=['GET'])
def get_user_target_companies(user_id: str):
    """List the Greenhouse board slugs this user wants scraped (see GreenhouseScraper in
    script.py -- it has no cross-company search, so users opt in per company)."""
    try:
        return jsonify({"success": True, "companies": db_store.get_target_companies(user_id)}), 200
    except Exception as e:
        logger.error(f"Error listing target companies: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/users/<user_id>/target-companies', methods=['POST'])
def add_user_target_company(user_id: str):
    try:
        data = request.get_json() or {}
        slug = (data.get('slug') or data.get('company_slug') or '').strip().lower()
        if not slug:
            return jsonify({"error": "slug is required"}), 400
        if not re.match(r'^[a-z0-9][a-z0-9-]{0,63}$', slug):
            return jsonify({"error": "slug must be the Greenhouse board token from the company's careers URL "
                                       "(boards.greenhouse.io/<slug>) -- lowercase letters, numbers and hyphens only"}), 400

        db_store.ensure_user(user_id)
        db_store.add_target_company(user_id, slug)
        return jsonify({"success": True, "companies": db_store.get_target_companies(user_id)}), 200
    except Exception as e:
        logger.error(f"Error adding target company: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/users/<user_id>/target-companies/<path:company_slug>', methods=['DELETE'])
def remove_user_target_company(user_id: str, company_slug: str):
    try:
        db_store.remove_target_company(user_id, company_slug)
        return jsonify({"success": True, "companies": db_store.get_target_companies(user_id)}), 200
    except Exception as e:
        logger.error(f"Error removing target company: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/users/<user_id>/email-status/<urn>', methods=['GET'])
def get_user_email_status(user_id: str, urn: str):
    try:
        lead = db_store.get_lead_by_urn(user_id, urn)
        if not lead:
            return jsonify({"error": f"No lead found with URN {urn}"}), 404

        return jsonify({
            "urn": urn,
            "email_sent": bool(lead.get('email_sent', False)),
            "email_sent_at": lead.get('email_sent_at'),
            "application_contact": lead.get('application_contact'),
            "template_type": lead.get('email_template_type')
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/users/<user_id>/dashboard/summary', methods=['GET'])
def get_user_dashboard_summary(user_id: str):
    try:
        stats = db_store.get_dashboard_stats(user_id)
        trends = db_store.get_daily_trends(user_id)

        activities = []
        for lead in db_store.get_recent_activity(user_id):
            job_title = lead.get('job_title') or 'Unknown Role'
            company_name = lead.get('company_name') or 'Unknown Company'
            created_at = lead.get('created_at')
            time_str = 'Unknown'
            if created_at:
                try:
                    time_str = datetime.fromisoformat(created_at).strftime('%H:%M')
                except ValueError:
                    pass
            activities.append({
                "action": f"New lead processed: {job_title}",
                "user": str(company_name),
                "time": time_str,
                "avatar": None,
                "data_ai_hint": f"Lead from {company_name}"
            })

        response_data = {
            "success": True,
            "stats": stats,
            "trends": trends,
            "recent_activity": activities
        }

        return jsonify(clean_nan_values(response_data)), 200
    except Exception as e:
        logger.error(f"Error fetching dashboard summary: {e}")
        return jsonify({"error": str(e)}), 500

# ----------------- CENTRAL SCRAPE ENGINE (BACKGROUND THREAD) -----------------

GLOBAL_EXTRACTION_PROMPT_VERSION = "global-v1"
_last_user_matching_metrics: Dict[str, int] = {"evaluated": 0, "matched": 0, "leads_created": 0}
STRUCTURED_SOURCES = {
    "jobs", "indeed", "glassdoor", "himalayas", "remoteok", "remotive", "jobicy",
    "arbeitnow", "hackernews", "weworkremotely", "greenhouse", "wellfound", "adzuna",
}


def _global_prefilter(item: Dict) -> tuple[bool, str]:
    """Conservative, user-independent filter used before any global LLM call."""
    text = str(item.get("text") or item.get("description") or "").strip()
    if not text:
        return False, "empty_text"
    if not item.get("_raw_item_id") or not (item.get("urn") or item.get("_urn")):
        return False, "malformed_record"
    return True, "eligible"


def _structured_lead(item: Dict) -> ExtractedLead:
    """Normalize source-provided job fields without spending an LLM call."""
    text = str(item.get("text") or "")
    title = item.get("title") or item.get("jobTitle") or None
    company = item.get("companyName") or item.get("company") or item.get("authorName") or None
    location = item.get("location") or item.get("jobLocation") or item.get("authorHeadline") or None
    url = item.get("url") or item.get("jobUrl") or item.get("applyUrl") or None
    email_match = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)
    method = "email" if email_match else ("link" if url else "other")
    contact = email_match.group(0) if email_match else url
    lower = f"{title or ''} {text}".lower()
    internship = "intern" in lower or "trainee" in lower
    fresher = internship or any(x in lower for x in ("fresher", "new grad", "entry-level", "entry level"))
    level = "Internship" if internship else ("Fresher" if fresher else None)
    return ExtractedLead(
        post_urn=str(item.get("urn") or item.get("_urn")), application_method=method,
        posting_date=str(item.get("postedAtISO") or item.get("postedAt") or ""),
        author_name=str(item.get("authorName") or company or ""),
        author_profile=str(item.get("authorProfileUrl") or ""), is_job_posting=True,
        post_category="job", job_title=title, company_name=company, location=location,
        work_mode="Remote" if (str(location or "").lower() == "remote" or "remote" in lower) else None,
        experience_level=[0, 0] if fresher else None, application_contact=contact, post_url=url,
        role_level=level, is_internship=internship, is_fresher=fresher,
        source=item.get("_source"), company_logo=item.get("companyLogo") or None,
    )


def _lead_from_dict(data: Dict) -> ExtractedLead:
    fields = {f.name for f in ExtractedLead.__dataclass_fields__.values()}
    return ExtractedLead(**{k: v for k, v in data.items() if k in fields})


def _lead_matches_user(lead: ExtractedLead, pref: PreferenceManager) -> tuple[bool, Dict[str, Any]]:
    """Cheap matching only; no candidate/private settings enter global extraction."""
    if not lead.is_job_posting:
        return False, {"reason": "not_job"}
    title = (lead.job_title or "").lower()
    haystack = " ".join([title, lead.company_name or "", *(lead.tech_stack or []), *(lead.skills_required or [])]).lower()
    roles = [r.lower().strip() for r in pref.preferred_roles() if r.strip()]
    categories = pref.categories()
    role_hit = any(r in haystack or any(token in haystack for token in r.split() if len(token) > 2) for r in roles)
    category = None
    for key, data in categories.items():
        rules = str(data.get("rules", "")).lower()
        if key.lower() in haystack or any(token in haystack for token in re.findall(r"[a-zA-Z]{3,}", rules)):
            category, role_hit = key, True
            break
    if roles or categories:
        if not role_hit:
            return False, {"reason": "role"}
    custom = pref.custom_instructions().lower()
    excluded = []
    for marker in ("exclude", "avoid", "not interested in"):
        if marker in custom:
            excluded.extend(re.split(r"[,;]", custom.split(marker, 1)[1])[:5])
    if any(term.strip() and term.strip() in haystack for term in excluded):
        return False, {"reason": "custom_instruction"}
    locations = [x.lower().strip() for x in pref.preferred_locations() if x.strip()]
    actual_location = (lead.location or "").lower()
    location_hit = not locations or not actual_location or any(x in actual_location or actual_location in x for x in locations)
    if not location_hit and "remote" in locations and lead.work_mode == "Remote":
        location_hit = True
    if not location_hit:
        return False, {"reason": "location"}
    if lead.role_level and lead.role_level.lower() in {"senior", "mid-level", "lead", "principal"}:
        return False, {"reason": "seniority"}
    reasons = ["job posting", "role/category match"]
    if location_hit:
        reasons.append("location match")
    return True, {"category": category, "match_reasons": reasons, "score": 1.0}


def run_global_extraction(limit: Optional[int] = None) -> Dict[str, int]:
    """Normalize and extract each shared raw item once, independent of users."""
    raw = db_store.get_global_extraction_candidates(limit=limit)
    metrics = {"candidates": len(raw), "skipped_by_filter": 0, "already_extracted": 0,
               "llm_items": 0, "llm_successes": 0, "llm_failures": 0, "structured_items": 0}
    # This is cumulative cache visibility, useful for cron logs even when this run
    # has no new candidates. Candidates themselves exclude completed rows.
    metrics["already_extracted"] = db_store.count_completed_global_extractions()
    if not raw:
        metrics.update(db_store.get_global_extraction_metrics())
        return metrics
    normalizer = LinkedInRunner()
    normalized = normalizer.normalize_bucketed_items(raw)
    by_id = {item.get("_raw_item_id"): item for item in normalized}
    llm_runner = None
    for raw_item in raw:
        raw_id = raw_item.get("_raw_item_id")
        item = by_id.get(raw_id, raw_item)
        ok, reason = _global_prefilter(item)
        if not ok:
            metrics["skipped_by_filter"] += 1
            db_store.save_global_extraction(raw_id, raw_item.get("_urn") or raw_item.get("urn"),
                                            {"_skipped": True, "skip_reason": reason})
            continue
        urn = raw_item.get("_urn") or raw_item.get("urn")
        if not db_store.claim_global_extraction(raw_id, urn):
            metrics["already_extracted"] += 1
            continue
        try:
            if item.get("_source") in STRUCTURED_SOURCES:
                lead = _structured_lead(item)
                metrics["structured_items"] += 1
            else:
                if llm_runner is None:
                    llm_runner = LinkedInRunner()
                    llm_runner._initialize_extractor()
                # Critical invariant: this method never runs user preference filtering.
                # The old process_single_post() path performed a preference-dependent LLM
                # filter before extraction and therefore could not be shared safely.
                lead, _ = llm_runner.extractor.extract_single_post_globally(item)
                if lead is None:
                    raise ValueError("extractor returned no result")
                metrics["llm_items"] += 1
            payload = asdict(lead)
            payload["_raw_item_id"] = raw_id
            db_store.save_global_extraction(raw_id, urn, payload)
            metrics["llm_successes"] += 1 if item.get("_source") not in STRUCTURED_SOURCES else 0
        except Exception as exc:
            metrics["llm_failures"] += 1
            logging.exception("Global extraction failed for %s", urn)
            db_store.save_global_extraction(raw_id, urn, None, error=str(exc))
    return metrics


def run_extraction_for_user(user_id: str, limit: Optional[int] = None) -> int:
    """Match cached global facts to one user and save only that user's leads."""
    items = db_store.get_completed_global_extractions()
    if limit:
        items = items[:limit]
    if not items:
        return 0
    pref = PreferenceManager(user_id=user_id)
    matched = []
    evaluated = 0
    raw_ids_by_urn = {}
    for item in items:
        if item.get("_skipped"):
            continue
        lead = _lead_from_dict(item)
        raw_ids_by_urn[str(lead.post_urn)] = item.get("_raw_item_id")
        evaluated += 1
        is_match, match_data = _lead_matches_user(lead, pref)
        raw_id = item.get("_raw_item_id")
        if is_match:
            valid_categories = list(pref.categories().keys())
            lead.email_template_type = match_data.get("category") or (valid_categories[0] if valid_categories else "software_dev")
            matched.append(lead)
        if raw_id:
            db_store.mark_attempted(user_id, raw_id, matched=is_match)
    inserted = db_store.save_leads(user_id, matched, raw_ids_by_urn)
    _last_user_matching_metrics.update({"evaluated": evaluated, "matched": len(matched), "leads_created": inserted})
    logging.info("User %s matching: evaluated=%d matched=%d leads_created=%d", user_id, evaluated, len(matched), inserted)
    return len(items)


def provision_user_leads(user_id: str):
    """Give a user immediate leads from the existing raw_items backlog (new signup, or a
    returning user who logged in between scrape cycles). Runs in a background thread."""
    try:
        logging.info(f"🔬 Provisioning leads for user {user_id} from existing raw_items backlog...")
        run_global_extraction()
        count = run_extraction_for_user(user_id)
        logging.info(f"✅ Provisioning complete for {user_id}: {count} backlog items processed")
    except Exception as e:
        logging.error(f"Failed to provision leads for user {user_id}: {e}")


def execute_pipeline_background(search_urls: List[str], limit: int, scrape_jobs: bool,
                                  scrape_indeed: bool = False, indeed_input: Optional[Dict] = None,
                                  scrape_glassdoor: bool = False, glassdoor_input: Optional[Dict] = None,
                                  scrape_remote_boards: bool = False, extra_scrapers: Optional[List[str]] = None):
    """Unified Background pipeline execution"""
    logging.info("🚀 Background pipeline thread started...")
    _update_pipeline_status(
        running=True, stage="scraping", started_at=datetime.now().isoformat(),
        finished_at=None, raw_items_scraped=0, raw_items_new=0,
        users_total=0, users_processed=0, current_user=None, error=None,
    )
    try:
        # Step 1: Run scrapers globally to fetch new posts
        global_runner = LinkedInRunner()
        # Target companies are per-user (Greenhouse has no cross-company search -- each company's
        # postings live at their own board token). Union across every user here so the single
        # global scrape covers everyone; normal per-user LLM filtering still decides relevance.
        all_target_companies = db_store.get_all_target_companies()
        search_params = {
            "search_urls": search_urls,
            "limit": limit,
            "indeed_input": indeed_input,
            "glassdoor_input": glassdoor_input,
            "target_companies": all_target_companies,
        }

        # Trigger scraper execution across whichever sources are enabled
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

        logging.info(f"Background executing scraper manager: {scrapers_to_run}")
        from script import scraper_registry
        raw_items = scraper_registry.scrape_all(global_runner, search_params, scrapers_to_run)

        # Step 2: Persist raw items to SQLite (single shared pool, deduped by urn)
        newly_stored = db_store.insert_raw_items(raw_items, source="posts")
        logging.info(f"Global scraping finished. Got {len(raw_items)} raw items ({newly_stored} new).")
        _update_pipeline_status(stage="extracting", raw_items_scraped=len(raw_items), raw_items_new=newly_stored)

        # Global extraction is deliberately completed before user processing. The cache is
        # keyed by raw item identity, so the same post is never sent once per user.
        global_metrics = run_global_extraction()
        logging.info("Global extraction metrics: %s", global_metrics)
        _update_pipeline_status(metrics=global_metrics)

        # Step 4: Match the shared structured cache independently for each user.
        if os.path.exists(USERS_DIR):
            user_folders = [f for f in os.listdir(USERS_DIR) if os.path.isdir(USERS_DIR / f)]
            logging.info(f"Processing lead extraction for registered users: {user_folders}")
            _update_pipeline_status(users_total=len(user_folders))
            user_metrics = {"user_matches_evaluated": 0, "user_leads_created": 0}

            for user_id in user_folders:
                _update_pipeline_status(current_user=user_id)
                try:
                    logging.info(f"🔬 Running lead extraction for User: {user_id}...")
                    count = run_extraction_for_user(user_id)
                    user_metrics["user_matches_evaluated"] += _last_user_matching_metrics["evaluated"]
                    user_metrics["user_leads_created"] += _last_user_matching_metrics["leads_created"]
                    logging.info(f"✅ User {user_id} pipeline extraction complete! ({count} items)")
                except Exception as user_err:
                    logging.error(f"Failed pipeline extraction for User {user_id}: {user_err}")
                finally:
                    with _pipeline_status_lock:
                        pipeline_status["users_processed"] += 1
            global_metrics.update(user_metrics)
            _update_pipeline_status(metrics=global_metrics)

        logging.info("🎉 Background pipeline execution completed successfully for all users!")
        _update_pipeline_status(running=False, stage="done", finished_at=datetime.now().isoformat(), current_user=None)
    except Exception as pipeline_err:
        logging.error(f"❌ Background pipeline execution encountered fatal error: {pipeline_err}")
        _update_pipeline_status(
            running=False, stage="error", finished_at=datetime.now().isoformat(),
            error=str(pipeline_err), current_user=None,
        )

@app.route('/api/scrape/status', methods=['GET'])
def get_scrape_status():
    with _pipeline_status_lock:
        return jsonify(dict(pipeline_status)), 200

@app.route('/api/scrape', methods=['POST'])
def trigger_scrape():
    """Trigger background dual scraper execution and sequential user pipeline filtering"""
    try:
        if pipeline_status.get("running"):
            return jsonify({"success": False, "message": "A scrape is already running."}), 409

        data = request.get_json() or {}
        limit = int(data.get("limit", 15))
        scrape_jobs = bool(data.get("scrape_jobs", True))
        scrape_indeed = bool(data.get("scrape_indeed", False))
        indeed_input = data.get("indeed_input")
        scrape_glassdoor = bool(data.get("scrape_glassdoor", False))
        glassdoor_input = data.get("glassdoor_input")
        scrape_remote_boards = bool(data.get("scrape_remote_boards", False))
        allowed_extra_scrapers = {
            "arbeitnow", "hackernews", "weworkremotely", "greenhouse",
            "wellfound", "adzuna",
            # "builtin", "dice", "usajobs" removed -- US-based sources
        }
        extra_scrapers = [s for s in (data.get("extra_scrapers") or []) if s in allowed_extra_scrapers]

        # Resolve search URLs
        search_urls = data.get("search_urls")
        if not search_urls:
            search_urls = create_default_search_urls()

        # Spawn daemon execution thread to avoid timing out the REST client
        thread = threading.Thread(
            target=execute_pipeline_background,
            args=(search_urls, limit, scrape_jobs, scrape_indeed, indeed_input,
                  scrape_glassdoor, glassdoor_input, scrape_remote_boards, extra_scrapers),
            daemon=True
        )
        thread.start()

        return jsonify({
            "success": True,
            "message": "Scraping pipeline successfully triggered in the background",
            "search_urls_count": len(search_urls),
            "limit_per_source": limit,
            "dual_scraping_enabled": scrape_jobs,
            "indeed_scraping_enabled": scrape_indeed,
            "glassdoor_scraping_enabled": scrape_glassdoor,
            "remote_boards_scraping_enabled": scrape_remote_boards,
            "extra_scrapers_enabled": extra_scrapers
        }), 202
    except Exception as e:
        logger.error(f"Error triggering background scrape: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    # Run the server on port 5000
    app.run(port=5000, host="0.0.0.0", debug=False)
