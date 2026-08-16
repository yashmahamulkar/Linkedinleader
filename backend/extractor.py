import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Union, Tuple, Set, Callable
from dataclasses import dataclass, asdict, field
from datetime import datetime
import pandas as pd
import hashlib
import os
import logging
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Tuple, Optional, Union
from math import ceil
from datetime import timedelta
from time import sleep
# LangChain imports
from langchain_google_genai import ChatGoogleGenerativeAI
try:
    from langchain.prompts import ChatPromptTemplate
except ImportError:  # LangChain v1+ moved prompts to langchain_core
    from langchain_core.prompts import ChatPromptTemplate

try:
    from langchain.schema import HumanMessage
except ImportError:  # LangChain v1+ moved messages to langchain_core
    from langchain_core.messages import HumanMessage

try:
    from langchain.output_parsers import PydanticOutputParser
except ImportError:  # LangChain v1+ moved output parsers to langchain_core
    from langchain_core.output_parsers import PydanticOutputParser
from pydantic import BaseModel, Field


#import custom modules
from keymanager import KeyManager
from config import ConfigManager
from preferencemanager import PreferenceManager




# Load environment variables from .env file
load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

@dataclass
class ExtractedLead:
    """Data class to store extracted lead information"""
    # Required fields (no defaults)
    post_urn: str
    application_method: str  # 'email', 'link', 'dm', 'other'
    posting_date: str
    author_name: str
    author_profile: str
    is_job_posting: bool
    post_category: str  # 'job', 'educational', 'networking', 'other'

    # Optional fields with defaults
    job_title: Optional[str] = None
    company_name: Optional[str] = None
    location: Optional[str] = None
    work_mode: Optional[str] = None  # Remote, Hybrid, On-site
    experience_level: Optional[List[int]] = None  # [min_years, max_years], e.g. [0, 0] for freshers
    salary_range: Optional[str] = None
    tech_stack: List[str] = field(default_factory=list)
    skills_required: List[str] = field(default_factory=list)
    application_contact: Optional[str] = None  # email address or link
    post_url: Optional[str] = None  # Direct URL to the LinkedIn post
    duplicate_key: Optional[str] = None  # For duplicate detection
    email_template_type: Optional[str] = None  # software_dev, ai
    # New insights for internship/fresher focus
    role_level: Optional[str] = None  # Internship, Fresher, Entry-level, Junior
    is_internship: Optional[bool] = None
    is_fresher: Optional[bool] = None
    graduation_years: Optional[List[int]] = None
    internship_duration: Optional[str] = None  # e.g., 3 months, 6 months
    stipend_range: Optional[str] = None
    application_deadline: Optional[str] = None
    eligibility_criteria: Optional[str] = None
    company_logo: Optional[str] = None  # Job's companyLogo, or the post author's profile picture
    source: Optional[str] = None  # posts, jobs, indeed, glassdoor -- which scraper this lead came from

class LeadExtractionOutput(BaseModel):
    """Pydantic model for structured output parsing"""
    job_title: Optional[str] = Field(default=None, description="Job title mentioned in the post")
    company_name: Optional[str] = Field(default=None, description="Company name if mentioned")
    location: Optional[str] = Field(default=None, description="Job location if specified")
    work_mode: Optional[str] = Field(default=None, description="Remote, Hybrid, On-site, or not specified")
    experience_level: Optional[List[int]] = Field(default=None, description="Required years of experience as [min_years, max_years] integers, e.g. [0, 0], [2, 4], [5, 5]. Freshers/interns/entry-level roles with no prior experience required must be [0, 0]. If only a single number or a minimum is stated (e.g. '5+ years'), use that same value for both min and max, e.g. [5, 5]. Never free text like 'Mid-level' or 'Not Applicable'.")
    salary_range: Optional[str] = Field(default=None, description="Salary or stipend mentioned")
    tech_stack: List[str] = Field(default_factory=list, description="Technologies mentioned (Python, SQL, etc.)")
    skills_required: List[str] = Field(default_factory=list, description="Skills explicitly mentioned as required")
    application_method: str = Field(description="How to apply: email, link, dm, or other")
    application_contact: Optional[str] = Field(default=None, description="Email address or application link")
    is_job_posting: bool = Field(description="True if this is a job posting")
    post_category: str = Field(description="Category: job, educational, networking, other")
    # Added fields for insights
    role_level: Optional[str] = Field(default=None, description="Role level: Internship, Fresher, Entry-level, Junior, etc.")
    is_internship: Optional[bool] = Field(default=None, description="True if internship")
    is_fresher: Optional[bool] = Field(default=None, description="True if suitable for freshers/new grads")
    graduation_years: List[int] = Field(default_factory=list, description="Graduation years mentioned (e.g., 2026)")
    internship_duration: Optional[str] = Field(default=None, description="Internship duration if any")
    stipend_range: Optional[str] = Field(default=None, description="Stipend range if internship")
    application_deadline: Optional[str] = Field(default=None, description="Deadline date/phrase if mentioned")
    eligibility_criteria: Optional[str] = Field(default=None, description="Key eligibility constraints")

def _extract_text_content(content: Union[str, List]) -> str:
    """Normalize a LangChain message's .content into plain text.

    Newer langchain-google-genai responses (e.g. Gemini 3.x "thinking" models)
    return a list of content blocks (e.g. [{"type": "text", "text": "...", "extras": {...}}])
    instead of a plain string. PydanticOutputParser and str.strip() both require a string.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return str(content)


class LinkedInLeadExtractor:
    """Main class for extracting leads from LinkedIn posts using Gemini"""

    def __init__(self, gemini_keys_path: Optional[str] = None, model_name: str = "gemini-3.1-flash-lite",
                 preferred_roles: List[str] = None, preferred_locations: List[str] = None,
                 strict_filtering: bool = True, config_manager: ConfigManager = None, 
                 preference_manager: PreferenceManager = None, user_id: Optional[str] = None):
        self.user_id = user_id
        self.config_manager = config_manager or ConfigManager(user_id=user_id)
        self.preferences = preference_manager or PreferenceManager(user_id=user_id)
        
        # Resolve gemini keys
        resolved_gemini_keys_path = gemini_keys_path or self.config_manager.get("gemini_keys_path")
        if not resolved_gemini_keys_path or not os.path.exists(resolved_gemini_keys_path):
            resolved_gemini_keys_path = str(PROJECT_ROOT / "configs" / "gemini_keys.json")
            
        self.key_manager = KeyManager(resolved_gemini_keys_path, "gemini")

        available_keys = self.key_manager.get_available_keys()
        if not available_keys:
            raise ValueError("No Gemini API keys available")

        self.llms = []
        self.llm_to_key = {}  # Map LLM index to API key for quota tracking
        for api_key in available_keys:
            if not api_key:
                continue
            try:
                llm = ChatGoogleGenerativeAI(
                    model=model_name,
                    google_api_key=api_key,
                    temperature=0.1
                )
                llm_idx = len(self.llms)
                self.llms.append(llm)
                self.llm_to_key[llm_idx] = api_key
                logging.info(f"Initialized LLM with key: {api_key[:10]}...")
            except Exception as e:
                logging.warning(f"Failed to initialize LLM with key {api_key[:10]}...: {str(e)}")

        if not self.llms:
            raise ValueError("No valid API keys provided")

        self.current_llm_index = 0
        self.parser = PydanticOutputParser(pydantic_object=LeadExtractionOutput)
        
        self.preferred_roles = preferred_roles or self.preferences.preferred_roles()
        self.preferred_locations = preferred_locations or self.preferences.preferred_locations()
        self.strict_filtering = strict_filtering
        self.extraction_prompt = self._create_extraction_prompt()
        self.filtering_prompt = self._create_filtering_prompt()
        
        logging.info(f"LinkedInLeadExtractor initialized with {len(self.llms)} LLM instances for user: {user_id}")

    def _get_next_llm(self) -> ChatGoogleGenerativeAI:
        """Get next available LLM instance in round-robin fashion"""
        llm = self.llms[self.current_llm_index]
        self.current_llm_index = (self.current_llm_index + 1) % len(self.llms)
        return llm

    def _invoke_llm_with_quota(self, llm: ChatGoogleGenerativeAI, messages, token_estimate: int = 0):
        """Invoke LLM and track quota usage"""
        llm_idx = None
        for idx, stored_llm in enumerate(self.llms):
            if stored_llm == llm:
                llm_idx = idx
                break

        if llm_idx is not None:
            api_key = self.llm_to_key.get(llm_idx)
            if api_key:
                self.key_manager.track_request(api_key, token_estimate)

        return llm.invoke(messages)

    def _create_filtering_prompt(self) -> ChatPromptTemplate:
        """Create prompt for initial filtering based on user preferences"""
        categories_dict = self.preferences.categories()
        custom_instructions = self.preferences.custom_instructions()
        
        cat_descriptions = []
        cat_labels = []
        for key, cat in categories_dict.items():
            display = cat.get("display_name", key)
            rules = cat.get("rules", "")
            cat_descriptions.append(f'* Category Code: "{key}" (Description: {display}. Matching rules: {rules})')
            cat_labels.append(f'- "MATCH_{key}" - job role matches the "{display}" category criteria')

        cat_descriptions_str = "\n".join(cat_descriptions)
        cat_labels_str = "\n".join(cat_labels)
        
        custom_inst_str = f"USER CUSTOM INSTRUCTIONS:\n{custom_instructions}\n" if custom_instructions else ""

        filtering_template = f"""
        You are an expert at analyzing LinkedIn posts to determine if they match specific job preferences.

        ANALYZE this LinkedIn post and determine if it matches the user's preferences:

        POST TEXT: {{post_text}}
        AUTHOR: {{author_name}}
        AUTHOR HEADLINE: {{author_headline}}

        USER'S PREFERRED ROLES: {{preferred_roles}}
        USER'S PREFERRED LOCATIONS: {{preferred_locations}}

        {custom_inst_str}

        TASK: Determine if this post should be processed for lead extraction.

        USER'S CUSTOM JOB CATEGORIES:
        {cat_descriptions_str}

        STRICT MATCHING CRITERIA (ALL MUST BE MET):

        1. JOB ROLE MATCHING (MANDATORY):
           - MUST match one of the User's Custom Job Categories described above.
           - Look for EXACT role matches or closely related technical roles.
           - Strictly REJECT non-technical roles (Sales, Marketing, HR, QA, Support) unless development-focused.

        2. BANNED ORGANIZATIONS:
           - GA0 Group, Prodigy, TEN, Bharat Intern.
           - Reject if post mentions 'AICTE', 'MSME certified', or similar non-genuine schemes.

        3. LOCATION MATCHING (MANDATORY):
           - MUST match preferred locations. "Remote" is accepted if user prefers remote work.
           - City/State/Country must match user preferences. If no location specified, consider as "flexible".
           - Strictly REJECT if location explicitly does not match.

        4. POST TYPE VERIFICATION (MANDATORY):
           - MUST be a genuine job posting with hiring intent (keywords: "hiring", "recruiting", "vacancy", "position", "role", "internship", "intern").
           - REJECT educational, company updates, or general networking.

        5. SENIORITY FILTER (CRITICAL - MANDATORY):
           - ONLY match Internship or Fresher/Entry-level/Junior roles.
           - Prefer graduating student batches (e.g., 2026).
           - REJECT mid/senior roles (senior, lead, architect, principal, or experience requirements > 1-2 years).

        6. COMPENSATED ONLY:
           - Reject all unpaid roles.

        RESPONSE FORMAT:
        Return ONLY one of these responses:
        {cat_labels_str}
        - "NO_MATCH_ROLE" - job role doesn't match any custom job category
        - "NO_MATCH_LOCATION" - location doesn't match preferences
        - "NO_MATCH_NOT_JOB" - not a genuine job posting with hiring intent
        - "NO_MATCH_SENIORITY" - not internship/fresher level
        - "NO_MATCH_INSUFFICIENT_INFO" - insufficient information to determine

        BE EXTREMELY STRICT: Only accept posts that clearly meet ALL criteria.
        """
        return ChatPromptTemplate.from_template(filtering_template)

    def _create_extraction_prompt(self) -> ChatPromptTemplate:
        """Create the prompt template for entity extraction with enhanced instructions and strict classification rules"""
        categories_dict = self.preferences.categories()
        category_keys = list(categories_dict.keys())
        category_keys_str = ", ".join(category_keys)
        
        prompt_template = f"""
        You are a world-class Artificial Intelligence specialized in extracting high-precision structured recruitment leads from LinkedIn feed updates and career announcements.

        Analyze the provided LinkedIn post text, author details, and context to build a perfectly populated schema.

        === INPUT DATA ===
        POST TEXT: {{post_text}}
        AUTHOR: {{author_name}}
        AUTHOR HEADLINE: {{author_headline}}

        === EXTRACTION TASKS & DETAILED INSTRUCTIONS ===

        1. JOB TITLE & COMPANY NAME (MANDATORY & EXACT MATCH):
           - Extract the exact Job Title. If multiple roles are mentioned, prioritize:
             1. AI / ML Engineer, Data Scientist, NLP/CV Engineer
             2. Software Engineer, Software Developer, Python/Java Developer
             3. Full Stack, Backend, or Frontend Developer
             4. DevOps, SRE, or Cloud Engineer
           - If a specific role matches the user's preferences, pick THAT one.
           - Extract the Company Name cleanly (e.g. "Entrans Inc." or "Fast Dolphin"). Strip extraneous phrases like "hiring for" or "leading startup".

        2. LOCATION & WORK MODE:
           - Location: Identify the specific city, state, or country (e.g., "Hartford, CT", "Chennai, India"). If purely work-from-home, use "Remote".
           - Work Mode: Must be exactly one of: "Remote", "Hybrid", "On-site", or "not specified".

        3. CLASSIFICATION & POST TYPE SEPARATION (CRITICAL!):
           - `is_job_posting` (boolean):
             * Set to `True` ONLY if this represents a formal job listing, job search application, career portal link, ATS link (Greenhouse, Lever, Workday, etc.), or formal company job description.
             * Set to `False` if it is a conversational/feed post written by an individual (e.g., a founder, recruiter, or employee posting "My team is hiring, email me your resume", or general networking text).
           - `post_category` (string):
             * Must be "job" if it's a formal job opening or active recruitment announcement.
             * Must be "networking" if it's a post discussing career fairs, general hiring lists, or employee search.
             * Must be "educational" if sharing learning material.
             * Must be "other" for non-matching.

        4. APPLICATION METHOD & CONTACT INFO:
           - `application_method` (string): Must be exactly one of: "email", "link", "dm", or "other".
           - `application_contact` (string):
             * If method is "email", extract the clean, exact email address (e.g., "rosa.romero@fastdolphin.com").
             * If method is "link", extract the full URL.
             * Do NOT guess or mix these up. Ensure emails are fully valid string values.

        5. SENIORITY, FRESHER & INTERNSHIP INSIGHTS (HIGH PRECISION):
           - `role_level`: "Internship", "Fresher", "Entry-level", "Junior", "Mid-level", or "Senior".
           - `is_internship` (boolean): `True` if explicitly described as an internship, co-op, or trainee role.
           - `is_fresher` (boolean): `True` if fresh graduates, 0 years of experience, or specific college batches (e.g. 2025/2026 grads) are explicitly welcome.
           - `experience_level` (REQUIRED YEARS OF EXPERIENCE -- ALWAYS A [min, max] INTEGER PAIR, NEVER TEXT):
             * Internship, Fresher, or "0 years experience" roles -> `[0, 0]`. This is the default whenever no experience requirement is stated.
             * An explicit range like "2-4 years" -> `[2, 4]`.
             * A single number or open-ended minimum like "5+ years" or "at least 3 years" -> repeat it for both, e.g. `[5, 5]` or `[3, 3]`.
             * Do NOT output words like "Mid-level", "Senior", or "Not Applicable" here -- that belongs in `role_level`, not `experience_level`.
           - `graduation_years` (list of integers): Extract any specific graduation year milestones mentioned (e.g., [2025, 2026]).
           - `stipend_range` or `salary_range`: Extract exact financial figures mentioned (e.g., "30K Per Month" or "$80,000 - $100,000").
           - `internship_duration`: Clean duration string if applicable (e.g. "3 months", "6 months").
           - `eligibility_criteria`: Explicit constraints like "Computer Science graduates only", "2026 batch", "No active backlogs".

        6. TECHNICAL STACKS & SKILLS:
           - `tech_stack` (list of strings): Extract programming languages, frameworks, databases, and cloud services explicitly mentioned (e.g., ["Python", "Vue 3", "AWS", "PostgreSQL"]).
           - `skills_required` (list of strings): Soft or hard skills required (e.g., ["Agile", "Scrum", "CI/CD", "Testing"]).

        === CRITICAL EXTRACTION RULES ===
        - Extract ONLY facts explicitly stated in the text.
        - Never invent, assume, or infer missing values. If a field is not present, use the default null/empty representation.
        - Normalize all strings to clean, readable formats (e.g., strip emoji, excessive spacing).

        {{format_instructions}}
        """
        return ChatPromptTemplate.from_template(prompt_template)

    def _create_global_extraction_prompt(self) -> ChatPromptTemplate:
        """Build the user-independent prompt used by the shared extraction cache.

        This deliberately contains no preferences, candidate details, resume data,
        categories, or email settings.  Those belong to the later user-matching stage.
        """
        return ChatPromptTemplate.from_template(f"""
        Extract factual job/post information from this LinkedIn post.
        Do not decide whether it matches any candidate. Do not use user preferences.
        Return only facts explicitly present in the post; use null/empty values when absent.

        POST TEXT: {{post_text}}
        AUTHOR: {{author_name}}
        AUTHOR HEADLINE: {{author_headline}}

        Classify whether this is a genuine job or recruitment post. Preserve the distinction
        between formal job postings and general networking/educational content.
        Extract title, company, location, work mode, experience, salary, skills, application
        method/contact, URL, internship/fresher indicators, graduation years, eligibility,
        and the post category using the output schema below.

        {{format_instructions}}
        """)

    def extract_single_post_globally(self, post_data: Dict) -> Tuple[Optional[ExtractedLead], str]:
        """Extract one post without invoking preference filtering or using user data.

        This is the only LLM path used by the global extraction cache.  It makes one
        extraction request per claimed raw item (with the existing key retry behavior).
        """
        if not post_data.get("text"):
            return None, "NO_TEXT"

        prompt = self._create_global_extraction_prompt().format(
            post_text=post_data.get("text", ""),
            author_name=post_data.get("authorName", ""),
            author_headline=post_data.get("authorHeadline", ""),
            format_instructions=self.parser.get_format_instructions(),
        )
        extracted_data = None
        error = None
        for _ in range(max(1, len(self.llms))):
            try:
                llm = self._get_next_llm()
                response = self._invoke_llm_with_quota(llm, [HumanMessage(content=prompt)], token_estimate=500)
                response_text = _extract_text_content(response.content).strip()
                try:
                    extracted_data = self.parser.parse(response_text)
                except Exception:
                    # Some models return a JSON array when a roundup post contains
                    # several jobs, despite the single-item schema. Keep the raw item
                    # globally cacheable and preserve the first factual result rather
                    # than marking the whole post failed and retrying it every cron run.
                    cleaned = response_text
                    if cleaned.startswith("```"):
                        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE | re.DOTALL).strip()
                    payload = json.loads(cleaned)
                    if isinstance(payload, list):
                        if not payload or not isinstance(payload[0], dict):
                            raise ValueError("global extraction returned an empty/non-object list")
                        logging.warning(
                            "Global extraction returned %d jobs for %s; using the first structured result",
                            len(payload), post_data.get("urn", "unknown"),
                        )
                        payload = payload[0]
                    extracted_data = self.parser.parse(json.dumps(payload, ensure_ascii=False))
                break
            except Exception as exc:
                error = exc
        if extracted_data is None:
            logging.warning("Global extraction failed for %s: %s", post_data.get("urn", "unknown"), error)
            return None, f"EXTRACTION_ERROR: {error}"

        text = post_data.get("text", "") or ""
        contact_info = self.extract_contact_info(text)
        text_lower = text.lower()
        year_matches = re.findall(r"\b(202[4-9])\b", text_lower)
        is_intern = "intern" in text_lower or "internship" in text_lower
        is_fresher = any(term in text_lower for term in (
            "fresher", "new grad", "newgraduate", "entry level", "entry-level",
            "0-1 year", "0 to 1 year",
        ))
        post_url = (post_data.get("url") or post_data.get("postUrl") or
                    post_data.get("linkedinUrl") or post_data.get("permalink"))
        return ExtractedLead(
            post_urn=str(post_data.get("urn") or post_data.get("_urn") or ""),
            job_title=extracted_data.job_title,
            company_name=extracted_data.company_name,
            location=extracted_data.location,
            work_mode=extracted_data.work_mode,
            experience_level=extracted_data.experience_level,
            salary_range=extracted_data.salary_range,
            tech_stack=extracted_data.tech_stack,
            skills_required=extracted_data.skills_required,
            application_method=contact_info["method"],
            application_contact=contact_info["primary_contact"],
            posting_date=post_data.get("postedAtISO", ""),
            author_name=post_data.get("authorName", ""),
            author_profile=post_data.get("authorProfileUrl", ""),
            post_url=post_url,
            is_job_posting=extracted_data.is_job_posting,
            post_category=extracted_data.post_category,
            role_level=getattr(extracted_data, "role_level", None),
            is_internship=getattr(extracted_data, "is_internship", None) or (True if is_intern else None),
            is_fresher=getattr(extracted_data, "is_fresher", None) or (True if is_fresher else None),
            graduation_years=getattr(extracted_data, "graduation_years", None) or ([int(y) for y in year_matches] or None),
            internship_duration=getattr(extracted_data, "internship_duration", None),
            stipend_range=getattr(extracted_data, "stipend_range", None),
            application_deadline=getattr(extracted_data, "application_deadline", None),
            eligibility_criteria=getattr(extracted_data, "eligibility_criteria", None),
            company_logo=post_data.get("companyLogo") or post_data.get("authorProfilePicture"),
            source=post_data.get("_source"),
        ), "GLOBAL_EXTRACTED"

    def extract_contact_info(self, text: str) -> Dict[str, Union[str, List[str]]]:
        """Extract email addresses and links from text"""
        email_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
        url_pattern = r'https?://[^\s]+'

        emails = re.findall(email_pattern, text)
        urls = re.findall(url_pattern, text)

        return {
            'emails': emails,
            'urls': urls,
            'primary_contact': emails[0] if emails else (urls[0] if urls else None),
            'method': 'email' if emails else ('link' if urls else 'other')
        }

    def _generate_duplicate_key(self, company_name: str, job_title: str) -> str:
        """Generate a unique key for duplicate detection based on company and role"""
        normalized_company = (company_name or "unknown").lower().strip()
        normalized_title = (job_title or "unknown").lower().strip()

        company_clean = re.sub(r'\b(ltd|limited|inc|incorporated|corp|corporation|llc|pvt)\b', '', normalized_company)
        title_clean = re.sub(r'\b(senior|junior|lead|principal|intern|trainee)\b', '', normalized_title)

        key_string = f"{company_clean.strip()}_{title_clean.strip()}"
        return hashlib.md5(key_string.encode()).hexdigest()[:12]

    def _select_preferred_role(self, job_title: str, preferred_roles: List[str]) -> str:
        """When a posting's title enumerates multiple distinct roles (e.g. "Backend / Data
        Engineer" or "Software Engineer, Data Scientist"), pick whichever one matches the
        user's preferences. A single-role title is always returned untouched -- it must never
        be replaced by a preference label, since that would silently corrupt the real title
        (e.g. a real "Software Engineer" listing displaying as "Software Developer" just
        because that happens to be the user's preferred-role string).
        """
        if not job_title or not preferred_roles:
            return job_title

        role_candidates = [c.strip() for c in re.split(r'\s*(?:/|,|\bor\b)\s*', job_title, flags=re.IGNORECASE) if c.strip()]
        if len(role_candidates) <= 1:
            return job_title

        for role in preferred_roles:
            role_lower = role.lower()
            for candidate in role_candidates:
                candidate_lower = candidate.lower()
                if role_lower in candidate_lower or candidate_lower in role_lower:
                    return candidate

        return job_title

    def _determine_email_template_type(self, lead: ExtractedLead) -> str:
        """Determine which email template to use based on job title and tech stack"""
        job_title = (lead.job_title or "").lower()
        tech_stack = [tech.lower() for tech in (lead.tech_stack or [])]

        ai_keywords = ['ai', 'artificial intelligence', 'machine learning', 'ml', 'data scien',
                      'deep learning', 'nlp', 'computer vision', 'llm', 'generative ai']
        if any(keyword in job_title for keyword in ai_keywords) or \
           any(keyword in ' '.join(tech_stack) for keyword in ['tensorflow', 'pytorch', 'sklearn', 'pandas', 'numpy']):
            return 'ai'

        dev_keywords = ['developer', 'engineer', 'programmer', 'software', 'backend', 'frontend', 'fullstack', 'full stack', 'devops', 'cloud']
        if any(keyword in job_title for keyword in dev_keywords) or \
           any(keyword in ' '.join(tech_stack) for keyword in ['python', 'java', 'javascript', 'react', 'node']):
            return 'software_dev'

        return 'software_dev'

    def prepare_unique_lead(self, lead: ExtractedLead, seen_keys: Set[str]) -> bool:
        """Assign lead.duplicate_key + a valid lead.email_template_type, then check `lead`
        against `seen_keys` -- returns True and adds the key if unique, False if it's a dup.

        Shared by remove_duplicates (whole-batch pass) and ColdEmailSystem's per-lead streaming
        callback (saves each lead to DB the moment its own LLM call finishes, not after the
        whole batch) so both apply identical dedup/categorization rules. `seen_keys` is mutated
        in place -- if this is ever called from multiple threads against the same set (as the
        streaming path does), the caller MUST hold a lock around the call, since the
        check-then-add here is not atomic on its own.
        """
        duplicate_key = self._generate_duplicate_key(lead.company_name, lead.job_title)
        lead.duplicate_key = duplicate_key
        if duplicate_key in seen_keys:
            return False

        if not lead.email_template_type:
            lead.email_template_type = self._determine_email_template_type(lead)
        valid_categories = list(self.preferences.categories().keys())
        if lead.email_template_type not in valid_categories:
            lead.email_template_type = valid_categories[0] if valid_categories else 'software_dev'

        seen_keys.add(duplicate_key)
        return True

    def remove_duplicates(self, leads: List[ExtractedLead],
                           existing_keys: Optional[Set[str]] = None) -> Tuple[List[ExtractedLead], Dict[str, int]]:
        """Remove duplicate entries based on company and role combination.

        `existing_keys` seeds the dedup set with duplicate_key fingerprints already saved to the
        DB in the last N days (see db.get_recent_duplicate_keys), so this catches cross-source/
        cross-run duplicates -- e.g. the same job posted on LinkedIn and Indeed with different
        post_urn values -- not just duplicates within this single batch.
        """
        unique_leads = []
        seen_keys = set(existing_keys or set())
        history_keys = set(existing_keys or set())
        duplicate_stats = {
            'total_leads': len(leads),
            'unique_leads': 0,
            'duplicates_removed': 0,
            'cross_run_duplicates_removed': 0,
            'duplicate_details': []
        }

        for lead in leads:
            if self.prepare_unique_lead(lead, seen_keys):
                unique_leads.append(lead)
                duplicate_stats['unique_leads'] += 1
            else:
                duplicate_stats['duplicates_removed'] += 1
                if lead.duplicate_key in history_keys:
                    duplicate_stats['cross_run_duplicates_removed'] += 1
                duplicate_stats['duplicate_details'].append({
                    'company': lead.company_name,
                    'job_title': lead.job_title,
                    'author': lead.author_name,
                    'duplicate_key': lead.duplicate_key
                })

        return unique_leads, duplicate_stats

    def should_process_post(self, post_data: Dict) -> Tuple[bool, str]:
        """Check if post matches user preferences before full extraction"""
        if not self.strict_filtering:
            return True, "FILTERING_DISABLED"

        if not self.preferred_roles and not self.preferred_locations:
            return True, "NO_PREFERENCES_SET"

        post_text = post_data.get('text', '')
        author_name = post_data.get('authorName', '')
        author_headline = post_data.get('authorHeadline', '')
        print(self.preferred_locations)
        print(self.preferred_roles)
        formatted_prompt = self.filtering_prompt.format(
            post_text=post_text,
            author_name=author_name,
            author_headline=author_headline,
            preferred_roles=', '.join(self.preferred_roles) if self.preferred_roles else 'Any role',
            preferred_locations=', '.join(self.preferred_locations) if self.preferred_locations else 'Any location'
        )

        try:
            llm = self._get_next_llm()
            response = self._invoke_llm_with_quota(llm, [HumanMessage(content=formatted_prompt)], token_estimate=100)
            result = _extract_text_content(response.content).strip()
            is_match = result.startswith("MATCH_")
            return is_match, result
        except Exception as e:
            print(f"Error in filtering post {post_data.get('urn', 'unknown')[:20]}: {e}")
            # Try another LLM if available
            for _ in range(len(self.llms) - 1):
                try:
                    llm = self._get_next_llm()
                    response = self._invoke_llm_with_quota(llm, [HumanMessage(content=formatted_prompt)], token_estimate=100)
                    result = _extract_text_content(response.content).strip()
                    is_match = result.startswith("MATCH_")
                    return is_match, result
                except Exception:
                    continue
            return True, "FILTERING_ERROR"

    def process_single_post(self, post_data: Dict) -> Tuple[Optional[ExtractedLead], str]:
        """Process a single LinkedIn post and extract lead information.

        Returns (lead_or_None, reason) -- the caller uses `reason` directly for stats instead
        of calling should_process_post() a second time, which would both double the LLM cost
        and risk a different (non-deterministic) answer than the one actually acted on here.
        """
        if not post_data.get('text'):
            print(f"Skipping post {post_data.get('urn', 'unknown')[:20]}: No text content")
            return None, "NO_TEXT"

        should_process, filter_reason = self.should_process_post(post_data)
        if not should_process:
            print(f"Skipping post {post_data.get('urn', 'unknown')[:20]}... - Reason: {filter_reason}")
            return None, filter_reason



        print(self.preferred_roles)
        print(self.preferred_locations)
        print(f"Processing post {post_data.get('urn', 'unknown')[:20]}... - Passed filtering")

        formatted_prompt = self.extraction_prompt.format(
            post_text=post_data.get('text', ''),
            author_name=post_data.get('authorName', ''),
            author_headline=post_data.get('authorHeadline', ''),
            format_instructions=self.parser.get_format_instructions()
        )

        try:
            llm = self._get_next_llm()
            response = self._invoke_llm_with_quota(llm, [HumanMessage(content=formatted_prompt)], token_estimate=500)
            extracted_data = self.parser.parse(_extract_text_content(response.content))
        except Exception as e:
            print(f"Error processing post {post_data.get('urn', 'unknown')}: {e}")
            # Try another LLM if available
            for _ in range(len(self.llms) - 1):
                try:
                    llm = self._get_next_llm()
                    response = self._invoke_llm_with_quota(llm, [HumanMessage(content=formatted_prompt)], token_estimate=500)
                    extracted_data = self.parser.parse(_extract_text_content(response.content))
                    break
                except Exception:
                    continue
            else:  # If all LLMs fail
                extracted_data = self._fallback_extraction(post_data)

        contact_info = self.extract_contact_info(post_data.get('text', ''))

        # Heuristics for internship/fresher insights
        text_lower = (post_data.get('text', '') or '').lower()
        year_matches = re.findall(r'\b(202[4-9])\b', text_lower)
        grad_years = [int(y) for y in year_matches]
        is_intern = ('intern' in text_lower) or ('internship' in text_lower)
        is_fresher = (
            'fresher' in text_lower or 'new grad' in text_lower or 'newgraduate' in text_lower or
            'entry level' in text_lower or 'entry-level' in text_lower or '0-1 year' in text_lower or '0 to 1 year' in text_lower
        )

        # Map filtering label to template type dynamically
        template_type = None
        if isinstance(filter_reason, str) and filter_reason.startswith("MATCH_"):
            cat_key = filter_reason.replace("MATCH_", "").strip()
            valid_categories = list(self.preferences.categories().keys())
            if cat_key in valid_categories:
                template_type = cat_key
            else:
                template_type = valid_categories[0] if valid_categories else 'software_dev'

        # Select preferred role if multiple roles mentioned
        selected_job_title = self._select_preferred_role(extracted_data.job_title, self.preferred_roles)

        # Extract post URL from various possible fields
        post_url = (
            post_data.get('url') or
            post_data.get('postUrl') or
            post_data.get('linkedinUrl') or
            post_data.get('permalink') or
            None
        )
        sleep(0.5)
        lead = ExtractedLead(
            post_urn=post_data.get('urn', ''),
            job_title=selected_job_title,
            company_name=extracted_data.company_name,
            location=extracted_data.location,
            work_mode=extracted_data.work_mode,
            experience_level=extracted_data.experience_level,
            salary_range=extracted_data.salary_range,
            tech_stack=extracted_data.tech_stack,
            skills_required=extracted_data.skills_required,
            application_method=contact_info['method'],
            application_contact=contact_info['primary_contact'],
            posting_date=post_data.get('postedAtISO', ''),
            author_name=post_data.get('authorName', ''),
            author_profile=post_data.get('authorProfileUrl', ''),
            post_url=post_url,
            is_job_posting=extracted_data.is_job_posting,
            post_category=extracted_data.post_category,
            email_template_type=template_type,
            role_level=getattr(extracted_data, 'role_level', None),
            is_internship=(getattr(extracted_data, 'is_internship', None) if hasattr(extracted_data, 'is_internship') else (True if is_intern else None)),
            is_fresher=(getattr(extracted_data, 'is_fresher', None) if hasattr(extracted_data, 'is_fresher') else (True if is_fresher else None)),
            graduation_years=(getattr(extracted_data, 'graduation_years', None) if hasattr(extracted_data, 'graduation_years') and getattr(extracted_data, 'graduation_years') else (grad_years or None)),
            internship_duration=getattr(extracted_data, 'internship_duration', None) if hasattr(extracted_data, 'internship_duration') else None,
            stipend_range=getattr(extracted_data, 'stipend_range', None) if hasattr(extracted_data, 'stipend_range') else None,
            application_deadline=getattr(extracted_data, 'application_deadline', None) if hasattr(extracted_data, 'application_deadline') else None,
            eligibility_criteria=getattr(extracted_data, 'eligibility_criteria', None) if hasattr(extracted_data, 'eligibility_criteria') else None,
            company_logo=post_data.get('companyLogo') or post_data.get('authorProfilePicture') or None,
            source=post_data.get('_source') or None
        )
        return lead, filter_reason

    def _fallback_extraction(self, post_data: Dict) -> LeadExtractionOutput:
        """Fallback extraction using regex patterns"""
        text = post_data.get('text', '').lower()
        is_job = any(keyword in text for keyword in ['hiring', 'intern', 'job', 'position', 'role', 'vacancy'])
        tech_keywords = ['python', 'java', 'javascript', 'react', 'sql', 'nodejs', 'django', 'flask']
        found_tech = [tech for tech in tech_keywords if tech in text]
        # internship/fresher heuristics
        role_level = None
        if 'intern' in text or 'internship' in text:
            role_level = 'Internship'
        elif 'fresher' in text or 'new grad' in text or 'entry level' in text or 'entry-level' in text:
            role_level = 'Fresher'
        year_matches = re.findall(r'\b(202[4-9])\b', text)
        grad_years = [int(y) for y in year_matches]
        experience_level = [0, 0] if role_level in ('Internship', 'Fresher') else None

        return LeadExtractionOutput(
            job_title=post_data.get('title'),
            company_name=None,
            location=None,
            work_mode=None,
            experience_level=experience_level,
            salary_range=None,
            tech_stack=found_tech,
            skills_required=[],
            application_method='other',
            application_contact=None,
            is_job_posting=is_job,
            post_category='job' if is_job else 'other',
            role_level=role_level,
            is_internship=True if role_level == 'Internship' else False if role_level else None,
            is_fresher=True if role_level == 'Fresher' else False if role_level else None,
            graduation_years=grad_years
        )

    def process_posts_subset(self, posts_subset: List[Dict], llm: ChatGoogleGenerativeAI,
                           start_idx: int, total_posts: int,
                           on_lead_extracted: Optional[Callable[[ExtractedLead, Dict], None]] = None
                           ) -> Tuple[List[ExtractedLead], Dict[str, int]]:
        """Process a subset of posts using a specific LLM instance"""
        extracted_leads = []
        filtering_stats = {
            'processed': 0,
            'skipped_role': 0,
            'skipped_location': 0,
            'skipped_not_job': 0,
            'skipped_insufficient_info': 0,
            'skipped_seniority': 0,
            'skipped_other': 0,
            'filtering_errors': 0,
            'extraction_errors': 0
        }

        for i, post in enumerate(posts_subset):
            global_idx = start_idx + i
            print(f"\n--- Processing post {global_idx + 1}/{total_posts} (Worker {self.llms.index(llm) + 1}) ---")
            try:
                # Override the _get_next_llm method temporarily
                original_get_next_llm = self._get_next_llm
                self._get_next_llm = lambda: llm

                lead, reason = self.process_single_post(post)

                # Restore original method
                self._get_next_llm = original_get_next_llm

                if lead:
                    extracted_leads.append(lead)
                    filtering_stats['processed'] += 1
                    if on_lead_extracted:
                        try:
                            on_lead_extracted(lead, post)
                        except Exception as callback_err:
                            print(f"on_lead_extracted callback failed for post {global_idx + 1}: {callback_err}")
                elif reason == "NO_MATCH_ROLE":
                    filtering_stats['skipped_role'] += 1
                elif reason == "NO_MATCH_LOCATION":
                    filtering_stats['skipped_location'] += 1
                elif reason == "NO_MATCH_NOT_JOB":
                    filtering_stats['skipped_not_job'] += 1
                elif reason == "NO_MATCH_SENIORITY":
                    filtering_stats['skipped_seniority'] += 1
                elif reason == "NO_MATCH_INSUFFICIENT_INFO":
                    filtering_stats['skipped_insufficient_info'] += 1
                elif reason == "FILTERING_ERROR":
                    filtering_stats['filtering_errors'] += 1
                else:
                    # Any other/unrecognized reason (e.g. "NO_TEXT", a future prompt label)
                    # still needs to be accounted for so per-run totals never silently drift.
                    filtering_stats['skipped_other'] += 1
            except Exception as e:
                print(f"Error processing post {global_idx + 1}: {e}")
                filtering_stats['extraction_errors'] += 1

        return extracted_leads, filtering_stats

    def process_posts_batch(self, posts_data: List[Dict],
                             on_lead_extracted: Optional[Callable[[ExtractedLead, Dict], None]] = None
                             ) -> Tuple[List[ExtractedLead], Dict[str, int]]:
        """Process a batch of LinkedIn posts in parallel using multiple API keys with dynamic chunking.

        `on_lead_extracted`, if given, fires once per successfully-extracted lead, right after
        its own LLM call finishes -- not after the whole batch. When parallel extraction is on,
        multiple chunks call this concurrently from different threads (see process_posts_subset),
        so it must be safe to call from any thread.
        """
        total_posts = len(posts_data)
        num_workers = len(self.llms)

        # Dynamic chunking based on config and post count
        max_posts_per_key = self.config_manager.get("max_posts_per_key", 500)
        optimal_chunk_size = min(max_posts_per_key, ceil(total_posts / num_workers))

        # Ensure we don't exceed the optimal chunk size
        if total_posts > num_workers * max_posts_per_key:
            logging.warning(f"Post count ({total_posts}) exceeds optimal capacity ({num_workers * max_posts_per_key}). Consider adding more keys.")

        # Split posts into chunks
        post_chunks = []
        for i in range(0, total_posts, optimal_chunk_size):
            chunk = posts_data[i:i + optimal_chunk_size]
            if chunk:  # Only add non-empty chunks
                post_chunks.append((chunk, i))

        all_leads = []
        combined_stats = {
            'total_posts': total_posts,
            'processed': 0,
            'skipped_role': 0,
            'skipped_location': 0,
            'skipped_not_job': 0,
            'skipped_insufficient_info': 0,
            'skipped_seniority': 0,
            'skipped_other': 0,
            'filtering_errors': 0,
            'extraction_errors': 0
        }

        logging.info(f"Starting parallel processing with {num_workers} workers")
        logging.info(f"Optimal chunk size: {optimal_chunk_size} posts per worker")
        logging.info(f"Total chunks: {len(post_chunks)}")

        if not self.config_manager.get("enable_parallel_extraction", True):
            logging.info("Parallel extraction disabled, processing sequentially")
            for chunk, start_idx in post_chunks:
                chunk_leads, chunk_stats = self.process_posts_subset(
                    chunk, self.llms[0], start_idx, total_posts, on_lead_extracted=on_lead_extracted
                )
                all_leads.extend(chunk_leads)
                for key in combined_stats:
                    if key != 'total_posts':
                        combined_stats[key] += chunk_stats[key]
        else:
            with ThreadPoolExecutor(max_workers=min(num_workers, len(post_chunks))) as executor:
                # Create futures for each chunk
                future_to_chunk = {
                    executor.submit(
                        self.process_posts_subset,
                        chunk,
                        self.llms[i % len(self.llms)],  # Cycle through available LLMs
                        start_idx,
                        total_posts,
                        on_lead_extracted
                    ): (chunk, i)
                    for i, (chunk, start_idx) in enumerate(post_chunks)
                }

                # Process completed futures
                for future in as_completed(future_to_chunk):
                    try:
                        chunk_leads, chunk_stats = future.result()
                        all_leads.extend(chunk_leads)

                        # Update combined statistics
                        for key in combined_stats:
                            if key != 'total_posts':
                                combined_stats[key] += chunk_stats[key]
                    except Exception as e:
                        logging.error(f"Error processing chunk: {e}")
                        combined_stats['extraction_errors'] += 1

        # Print summary statistics
        logging.info("="*50)
        logging.info("PROCESSING SUMMARY:")
        logging.info(f"Total Posts: {combined_stats['total_posts']}")
        logging.info(f"Successfully Processed: {combined_stats['processed']}")
        logging.info(f"Skipped - Role Mismatch: {combined_stats['skipped_role']}")
        logging.info(f"Skipped - Location Mismatch: {combined_stats['skipped_location']}")
        logging.info(f"Skipped - Not Job Posting: {combined_stats['skipped_not_job']}")
        logging.info(f"Skipped - Seniority Mismatch: {combined_stats.get('skipped_seniority', 0)}")
        logging.info(f"Skipped - Insufficient Info: {combined_stats['skipped_insufficient_info']}")
        logging.info(f"Skipped - Other: {combined_stats.get('skipped_other', 0)}")
        logging.info(f"Filtering Errors: {combined_stats['filtering_errors']}")
        logging.info(f"Extraction Errors: {combined_stats['extraction_errors']}")
        logging.info("="*50)

        return all_leads, combined_stats

    def filter_job_posts(self, leads: List[ExtractedLead]) -> List[ExtractedLead]:
        """Filter only job-related posts"""
        return [lead for lead in leads if lead.is_job_posting]


