from flask import Flask, request, jsonify
from flask_cors import CORS
import pandas as pd
import numpy as np
import os
import json
import logging

from datetime import datetime
from typing import Optional, Dict, Any

# Import your existing email infrastructure
try:
    from enhanced_emailservice import create_email_service_from_env
    from emailmanager import TemplateLoader, EmailTemplateGenerator
    from config import ConfigManager
    from extractor import ExtractedLead
except ImportError as e:
    print(f"Warning: Could not import email modules: {e}")
    print("Email functionality will be disabled")

app = Flask(__name__)

# Enable CORS for all routes
CORS(app, origins="*")

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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

# Configuration
CSV_FILE_PATH = r'/home/Lazycat/unique_filtered_leads.csv'
CONFIG_PATH = r'/home/Lazycat/mysite/configs/config.json'
LINKEDIN_JSON_PATH = r'/home/Lazycat/linkedin_data.json'

# Cache for LinkedIn JSON data
linkedin_data_cache = None

def load_linkedin_json_data():
    """Load and cache LinkedIn JSON data"""
    global linkedin_data_cache

    if linkedin_data_cache is not None:
        return linkedin_data_cache

    try:
        if os.path.exists(LINKEDIN_JSON_PATH):
            with open(LINKEDIN_JSON_PATH, 'r', encoding='utf-8') as f:
                linkedin_data_cache = json.load(f)
                logger.info(f"Loaded {len(linkedin_data_cache)} LinkedIn posts from JSON")
                # Create a URN-to-post mapping for faster lookups
                linkedin_data_cache = {post['urn']: post for post in linkedin_data_cache}
                return linkedin_data_cache
        else:
            logger.warning(f"LinkedIn JSON file not found: {LINKEDIN_JSON_PATH}")
            return {}
    except Exception as e:
        logger.error(f"Failed to load LinkedIn JSON data: {e}")
        return {}

# Initialize email components
try:
    config_manager = ConfigManager()
    template_loader = TemplateLoader()
    logger.info("Initializing email components", config_manager.get("candidate_name", "John Doe"))
    logger.info("Initializing email components", config_manager.get("candidate_email", "john.doe@email.com"))
    logger.info("Initializing email components", config_manager.get("resume_path", ""))

    email_generator = EmailTemplateGenerator(
        candidate_name=config_manager.get("candidate_name", "John Doe"),
        candidate_email=config_manager.get("candidate_email", "john.doe@email.com"),
        resume_path=config_manager.get("resume_path", "resumes/software_dev_resume.pdf"),
        template_loader=template_loader
    )
    email_service = create_email_service_from_env()
    EMAIL_ENABLED = email_service is not None
except Exception as e:
    logger.error(f"Failed to initialize email components: {e}")
    EMAIL_ENABLED = False

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({
        "status": "ok",
        "email_enabled": EMAIL_ENABLED,
        "csv_file_exists": os.path.exists(CSV_FILE_PATH)
    }), 200

@app.route('/leads', methods=['GET'])
def get_leads():
    try:
        df = pd.read_csv(CSV_FILE_PATH)
        leads_data = df.to_dict('records')

        # Load LinkedIn JSON data for text content
        linkedin_json_data = load_linkedin_json_data()

        # Enhanced leads data with additional formatting
        enhanced_leads = []
        for lead in leads_data:
            urn = lead.get('post_urn')

            # Get original LinkedIn post data if available
            linkedin_post = linkedin_json_data.get(urn, {})

            enhanced_lead = {
                # Core CSV data
                "csv_data": lead,

                # Original LinkedIn post data (includes text field)
                "linkedin_post_data": {
                    "text": linkedin_post.get('text', ''),
                    "title": linkedin_post.get('title', ''),
                    "url": linkedin_post.get('url', ''),
                    "posted_at": linkedin_post.get('postedAtISO', ''),
                    "author_headline": linkedin_post.get('authorHeadline', ''),
                    "is_repost": linkedin_post.get('isRepost', False),
                    "time_since_posted": linkedin_post.get('timeSincePosted', ''),
                    "input_url": linkedin_post.get('inputUrl', '')
                },

                # Formatted/enhanced fields for easier consumption
                "urn": urn,
                "job_info": {
                    "title": lead.get('job_title'),
                    "company": lead.get('company_name'),
                    "location": lead.get('location'),
                    "work_mode": lead.get('work_mode'),
                    "experience_level": lead.get('experience_level'),
                    "salary_range": lead.get('salary_range'),
                    "stipend_range": lead.get('stipend_range')
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

        # Summary statistics
        stats = {
            "total_leads": len(leads_data),
            "email_leads": len([l for l in leads_data if l.get('application_method') == 'email']),
            "link_leads": len([l for l in leads_data if l.get('application_method') == 'link']),
            "other_leads": len([l for l in leads_data if l.get('application_method') == 'other']),
            "emails_sent": len([l for l in leads_data if str(l.get('email_sent', '')).lower() == 'true']),
            "internships": len([l for l in leads_data if str(l.get('is_internship', '')).lower() == 'true']),
            "fresher_roles": len([l for l in leads_data if str(l.get('is_fresher', '')).lower() == 'true'])
        }

        # Clean NaN values from the data before sending
        response_data = {
            "success": True,
            "statistics": stats,
            "leads": enhanced_leads,
            "raw_csv_data": leads_data  # Original CSV data for backward compatibility
        }

        # Clean all NaN values
        cleaned_response = clean_nan_values(response_data)

        return jsonify(cleaned_response), 200

    except Exception as e:
        logger.error(f"Error in get_leads endpoint: {e}")
        return jsonify({"error": str(e)}), 500



@app.route('/leads-emails', methods=['GET'])
def get_email_leads():
    try:
        df = pd.read_csv(CSV_FILE_PATH)
        df =df[df['application_method']=='email']
        leads_data = df.to_dict('records')

        # Load LinkedIn JSON data for text content
        linkedin_json_data = load_linkedin_json_data()

        # Enhanced leads data with additional formatting
        enhanced_leads = []
        for lead in leads_data:
            urn = lead.get('post_urn')

            # Get original LinkedIn post data if available
            linkedin_post = linkedin_json_data.get(urn, {})

            enhanced_lead = {
                # Core CSV data
                "csv_data": lead,

                # Original LinkedIn post data (includes text field)
                "linkedin_post_data": {
                    "text": linkedin_post.get('text', ''),
                    "title": linkedin_post.get('title', ''),
                    "url": linkedin_post.get('url', ''),
                    "posted_at": linkedin_post.get('postedAtISO', ''),
                    "author_headline": linkedin_post.get('authorHeadline', ''),
                    "is_repost": linkedin_post.get('isRepost', False),
                    "time_since_posted": linkedin_post.get('timeSincePosted', ''),
                    "input_url": linkedin_post.get('inputUrl', '')
                },

                # Formatted/enhanced fields for easier consumption
                "urn": urn,
                "job_info": {
                    "title": lead.get('job_title'),
                    "company": lead.get('company_name'),
                    "location": lead.get('location'),
                    "work_mode": lead.get('work_mode'),
                    "experience_level": lead.get('experience_level'),
                    "salary_range": lead.get('salary_range'),
                    "stipend_range": lead.get('stipend_range')
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

        # Summary statistics
        stats = {
            "total_leads": len(leads_data),
            "email_leads": len([l for l in leads_data if l.get('application_method') == 'email']),
            "link_leads": len([l for l in leads_data if l.get('application_method') == 'link']),
            "other_leads": len([l for l in leads_data if l.get('application_method') == 'other']),
            "emails_sent": len([l for l in leads_data if str(l.get('email_sent', '')).lower() == 'true']),
            "internships": len([l for l in leads_data if str(l.get('is_internship', '')).lower() == 'true']),
            "fresher_roles": len([l for l in leads_data if str(l.get('is_fresher', '')).lower() == 'true'])
        }

        # Clean NaN values from the data before sending
        response_data = {
            "success": True,
            "statistics": stats,
            "leads": enhanced_leads,
            "raw_csv_data": leads_data  # Original CSV data for backward compatibility
        }

        # Clean all NaN values
        cleaned_response = clean_nan_values(response_data)

        return jsonify(cleaned_response), 200

    except Exception as e:
        logger.error(f"Error in get_leads endpoint: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/send-email', methods=['POST'])
def send_email():
    """
    Send email to a specific lead by URN

    Expected JSON payload:
    {
        "urn": "urn:li:activity:7374824703347118080"
    }
    """
    try:
        # Get URN from request
        data = request.get_json()
        if not data or 'urn' not in data:
            return jsonify({"error": "URN is required in request body"}), 400

        urn = data['urn']
        logger.info(f"Processing email request for URN: {urn}")

        # Read CSV file
        if not os.path.exists(CSV_FILE_PATH):
            return jsonify({"error": "CSV file not found"}), 404

        df = pd.read_csv(CSV_FILE_PATH)

        # Find matching row by URN
        matching_rows = df[df['post_urn'] == urn]
        if matching_rows.empty:
            return jsonify({"error": f"No lead found with URN: {urn}"}), 404

        # Get the first matching row
        lead_row = matching_rows.iloc[0]
        lead = create_extracted_lead_from_csv_row(lead_row)
        print("Email Template using : ",lead.email_template_type)
        # Check if email was already sent
        if lead_row.get('email_sent', False) == True or str(lead_row.get('email_sent', '')).lower() == 'true':
            return jsonify({
                "error": "Email already sent for this URN",
                "urn": urn,
                "email_sent_at": lead_row.get('email_sent_at', 'Unknown')
            }), 409

        # Check if it's an email lead (has application_contact)
        if lead_row['application_method'] != 'email' or pd.isna(lead_row['application_contact']) or not lead_row['application_contact']:
            return jsonify({
                "error": "This lead does not have email contact information",
                "urn": urn,
                "application_method": lead_row['application_method']
            }), 400

        # Check if email service is available
        if not EMAIL_ENABLED:
            return jsonify({"error": "Email service not configured"}), 503

        # Create ExtractedLead object for email generation

        # Generate email content
        email_content = email_generator.generate_email(lead)

        # Send email
        success = email_service.send_single_email(
            to_email=lead.application_contact,
            subject=email_content['subject'],
            body=email_content['body'],
            template_type=lead.email_template_type or 'software_dev'
        )

        if success:
            # Update CSV to mark email as sent
            update_email_sent_flag(urn, df)

            return jsonify({
                "success": True,
                "message": "Email sent successfully",
                "urn": urn,
                "email": lead.application_contact,
                "template_type": lead.email_template_type,
                "sent_at": datetime.now().isoformat()
            }), 200
        else:
            return jsonify({
                "error": "Failed to send email",
                "urn": urn,
                "email": lead.application_contact
            }), 500

    except Exception as e:
        logger.error(f"Error in send_email endpoint: {e}")
        return jsonify({"error": f"Internal server error: {str(e)}"}), 500


def create_extracted_lead_from_csv_row(row) -> ExtractedLead:
    return ExtractedLead(
        post_urn=row['post_urn'],
        application_method=row['application_method'],
        posting_date=row.get('posting_date'),
        author_name=row.get('author_name'),
        author_profile=row.get('author_profile'),
        is_job_posting=row.get('is_job_posting', True),
        post_category=row.get('post_category'),
        job_title=row.get('job_title'),
        company_name=row.get('company_name'),
        location=row.get('location'),
        work_mode=row.get('work_mode'),
        experience_level=row.get('experience_level'),
        salary_range=row.get('salary_range'),
        tech_stack=row.get('tech_stack'),
        skills_required=row.get('skills_required'),
        application_contact=row.get('application_contact'),
        post_url=row.get('post_url'),
        duplicate_key=row.get('duplicate_key'),
        email_template_type=row.get('email_template_type'),
        role_level=row.get('role_level'),
        is_internship=row.get('is_internship', False),
        is_fresher=row.get('is_fresher', False),
        graduation_years=row.get('graduation_years'),
        internship_duration=row.get('internship_duration'),
        stipend_range=row.get('stipend_range'),
        application_deadline=row.get('application_deadline'),
        eligibility_criteria=row.get('eligibility_criteria')
    )

def update_email_sent_flag(urn: str, df: pd.DataFrame) -> None:
    try:
        # Update the dataframe
        mask = df['post_urn'] == urn
        df.loc[mask, 'email_sent'] = True
        df.loc[mask, 'email_sent_at'] = datetime.now().isoformat()

        # Save back to CSV
        df.to_csv(CSV_FILE_PATH, index=False)
        logger.info(f"Updated email_sent flag for URN: {urn}")

    except Exception as e:
        logger.error(f"Failed to update email_sent flag for URN {urn}: {e}")
        raise

@app.route('/email-status/<urn>', methods=['GET'])
def get_email_status(urn: str):
    """Get email status for a specific URN"""
    try:
        if not os.path.exists(CSV_FILE_PATH):
            return jsonify({"error": "CSV file not found"}), 404

        df = pd.read_csv(CSV_FILE_PATH)
        matching_rows = df[df['post_urn'] == urn]

        if matching_rows.empty:
            return jsonify({"error": f"No lead found with URN: {urn}"}), 404

        lead_row = matching_rows.iloc[0]

        return jsonify({
            "urn": urn,
            "email_sent": bool(lead_row.get('email_sent', False)),
            "email_sent_at": lead_row.get('email_sent_at'),
            "application_contact": lead_row.get('application_contact'),
            "application_method": lead_row.get('application_method'),
            "template_type": lead_row.get('email_template_type'),
            "company_name": lead_row.get('company_name'),
            "job_title": lead_row.get('job_title')
        }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/dashboard/stats', methods=['GET'])
def get_dashboard_stats():
    """Get dashboard statistics"""
    try:
        if not os.path.exists(CSV_FILE_PATH):
            return jsonify({"error": "CSV file not found"}), 404

        df = pd.read_csv(CSV_FILE_PATH)

        # Calculate statistics
        total_leads = len(df)
        email_leads = len(df[df['application_method'] == 'email'])
        emails_sent = len(df[df['email_sent'].astype(str).str.lower() == 'true'])
        link_leads = len(df[df['application_method'] == 'link'])
        other_leads = len(df[df['application_method'] == 'other'])
        internships = len(df[df['is_internship'].astype(str).str.lower() == 'true'])
        fresher_roles = len(df[df['is_fresher'].astype(str).str.lower() == 'true'])

        # Calculate success rate (emails sent / email leads)
        success_rate = (emails_sent / email_leads * 100) if email_leads > 0 else 0

        # Recent activity (last 7 days)
        df['created_at'] = pd.to_datetime(df['created_at'], errors='coerce')
        recent_leads = len(df[df['created_at'] > pd.Timestamp.now() - pd.Timedelta(days=7)])

        stats = {
            "total_leads_processed": total_leads,
            "emails_sent": emails_sent,
            "success_rate": round(success_rate, 1),
            "email_leads": email_leads,
            "link_leads": link_leads,
            "other_leads": other_leads,
            "internships": internships,
            "fresher_roles": fresher_roles,
            "recent_leads": recent_leads
        }

        return jsonify({
            "success": True,
            "stats": stats
        }), 200

    except Exception as e:
        logger.error(f"Error in dashboard stats endpoint: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/dashboard/trends', methods=['GET'])
def get_dashboard_trends():
    """Get trends data for charts"""
    try:
        if not os.path.exists(CSV_FILE_PATH):
            return jsonify({"error": "CSV file not found"}), 404

        df = pd.read_csv(CSV_FILE_PATH)

        # Convert created_at to datetime
        df['created_at'] = pd.to_datetime(df['created_at'], errors='coerce')
        df['date'] = df['created_at'].dt.date

        # Group by date and calculate daily stats
        daily_stats = df.groupby('date').agg({
            'post_urn': 'count',  # Total leads
            'email_sent': lambda x: (x.astype(str).str.lower() == 'true').sum()  # Emails sent
        }).reset_index()

        daily_stats.columns = ['date', 'Leads', 'Emails']
        daily_stats['date'] = daily_stats['date'].astype(str)

        # Get last 30 days of data
        trends_data = daily_stats.tail(30).to_dict('records')

        return jsonify({
            "success": True,
            "trends": trends_data
        }), 200

    except Exception as e:
        logger.error(f"Error in dashboard trends endpoint: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/dashboard/recent-activity', methods=['GET'])
def get_recent_activity():
    """Get recent activity for dashboard"""
    try:
        if not os.path.exists(CSV_FILE_PATH):
            return jsonify({"error": "CSV file not found"}), 404

        df = pd.read_csv(CSV_FILE_PATH)

        # Convert datetime columns
        df['created_at'] = pd.to_datetime(df['created_at'], errors='coerce')
        df['email_sent_at'] = pd.to_datetime(df['email_sent_at'], errors='coerce')

        activities = []

        # Recent leads (last 10)
        recent_leads = df.nlargest(10, 'created_at')
        for _, lead in recent_leads.iterrows():
            # Clean NaN values before using them
            job_title = lead.get('job_title')
            job_title = 'Unknown Role' if pd.isna(job_title) else str(job_title)

            company_name = lead.get('company_name')
            company_name = 'Unknown Company' if pd.isna(company_name) else str(company_name)

            activities.append({
                "action": f"New lead processed: {job_title}",
                "user": company_name,
                "time": lead['created_at'].strftime('%H:%M') if pd.notna(lead['created_at']) else 'Unknown',
                "avatar": None,
                "data_ai_hint": f"Lead from {company_name}"
            })

        # Recent emails sent (last 5)
        recent_emails = df[df['email_sent'].astype(str).str.lower() == 'true'].nlargest(5, 'email_sent_at')
        for _, email in recent_emails.iterrows():
            # Clean NaN values before using them
            job_title = email.get('job_title')
            job_title = 'role' if pd.isna(job_title) else str(job_title)

            application_contact = email.get('application_contact')
            application_contact = 'Unknown Email' if pd.isna(application_contact) else str(application_contact)

            company_name = email.get('company_name')
            company_name = 'company' if pd.isna(company_name) else str(company_name)

            activities.append({
                "action": f"Email sent for {job_title}",
                "user": application_contact,
                "time": email['email_sent_at'].strftime('%H:%M') if pd.notna(email['email_sent_at']) else 'Unknown',
                "avatar": None,
                "data_ai_hint": f"Email sent to {company_name}"
            })

        # Sort by time and limit to 10 most recent
        activities = sorted(activities, key=lambda x: x['time'], reverse=True)[:10]

        # Clean any remaining NaN values
        cleaned_activities = clean_nan_values(activities)

        return jsonify({
            "success": True,
            "activities": cleaned_activities
        }), 200

    except Exception as e:
        logger.error(f"Error in recent activity endpoint: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/dashboard/summary', methods=['GET'])
def get_dashboard_summary():
    """Get complete dashboard data in one call"""
    try:
        # Get stats
        stats_response = get_dashboard_stats()
        if stats_response[1] != 200:
            return stats_response

        # Get trends
        trends_response = get_dashboard_trends()
        if trends_response[1] != 200:
            return trends_response

        # Get recent activity
        activity_response = get_recent_activity()
        if activity_response[1] != 200:
            return activity_response

        return jsonify({
            "success": True,
            "stats": stats_response[0].get_json()["stats"],
            "trends": trends_response[0].get_json()["trends"],
            "recent_activity": activity_response[0].get_json()["activities"]
        }), 200

    except Exception as e:
        logger.error(f"Error in dashboard summary endpoint: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/leads-manual', methods=['GET'])
def get_link_leads():
    try:
        df = pd.read_csv(CSV_FILE_PATH)
        df =df[df['application_method']=='link']
        leads_data = df.to_dict('records')

        # Load LinkedIn JSON data for text content
        linkedin_json_data = load_linkedin_json_data()

        # Enhanced leads data with additional formatting
        enhanced_leads = []
        for lead in leads_data:
            urn = lead.get('post_urn')

            # Get original LinkedIn post data if available
            linkedin_post = linkedin_json_data.get(urn, {})

            enhanced_lead = {
                # Core CSV data
                "csv_data": lead,

                # Original LinkedIn post data (includes text field)
                "linkedin_post_data": {
                    "text": linkedin_post.get('text', ''),
                    "title": linkedin_post.get('title', ''),
                    "url": linkedin_post.get('url', ''),
                    "posted_at": linkedin_post.get('postedAtISO', ''),
                    "author_headline": linkedin_post.get('authorHeadline', ''),
                    "is_repost": linkedin_post.get('isRepost', False),
                    "time_since_posted": linkedin_post.get('timeSincePosted', ''),
                    "input_url": linkedin_post.get('inputUrl', '')
                },

                # Formatted/enhanced fields for easier consumption
                "urn": urn,
                "job_info": {
                    "title": lead.get('job_title'),
                    "company": lead.get('company_name'),
                    "location": lead.get('location'),
                    "work_mode": lead.get('work_mode'),
                    "experience_level": lead.get('experience_level'),
                    "salary_range": lead.get('salary_range'),
                    "stipend_range": lead.get('stipend_range')
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

        # Summary statistics
        finalleads=enhanced_leads[::-1]
        stats = {
            "total_leads": len(leads_data),
            "email_leads": len([l for l in leads_data if l.get('application_method') == 'email']),
            "link_leads": len([l for l in leads_data if l.get('application_method') == 'link']),
            "other_leads": len([l for l in leads_data if l.get('application_method') == 'other']),
            "emails_sent": len([l for l in leads_data if str(l.get('email_sent', '')).lower() == 'true']),
            "internships": len([l for l in leads_data if str(l.get('is_internship', '')).lower() == 'true']),
            "fresher_roles": len([l for l in leads_data if str(l.get('is_fresher', '')).lower() == 'true'])
        }

        # Clean NaN values from the data before sending
        response_data = {
            "success": True,
            "statistics": stats,
            "leads": finalleads,
            "raw_csv_data": leads_data  # Original CSV data for backward compatibility
        }

        # Clean all NaN values
        cleaned_response = clean_nan_values(response_data)

        return jsonify(cleaned_response), 200

    except Exception as e:
        logger.error(f"Error in get_leads endpoint: {e}")
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    app.run(port=5000)