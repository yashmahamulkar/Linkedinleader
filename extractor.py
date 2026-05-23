import json
import re
from typing import Dict, List, Optional, Union, Tuple
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
from langchain.prompts import ChatPromptTemplate
from langchain.schema import HumanMessage
from langchain.output_parsers import PydanticOutputParser
from pydantic import BaseModel, Field


#import custom modules
from keymanager import KeyManager
from config import ConfigManager
from preferencemanager import PreferenceManager




# Load environment variables from .env file
load_dotenv()

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
    experience_level: Optional[str] = None  # Fresher, Junior, Senior, etc.
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

class LeadExtractionOutput(BaseModel):
    """Pydantic model for structured output parsing"""
    job_title: Optional[str] = Field(description="Job title mentioned in the post")
    company_name: Optional[str] = Field(description="Company name if mentioned")
    location: Optional[str] = Field(description="Job location if specified")
    work_mode: Optional[str] = Field(description="Remote, Hybrid, On-site, or not specified")
    experience_level: Optional[str] = Field(description="Experience level required")
    salary_range: Optional[str] = Field(description="Salary or stipend mentioned")
    tech_stack: List[str] = Field(description="Technologies mentioned (Python, SQL, etc.)")
    skills_required: List[str] = Field(description="Skills explicitly mentioned as required")
    application_method: str = Field(description="How to apply: email, link, dm, or other")
    application_contact: Optional[str] = Field(description="Email address or application link")
    is_job_posting: bool = Field(description="True if this is a job posting")
    post_category: str = Field(description="Category: job, educational, networking, other")
    # Added fields for insights
    role_level: Optional[str] = Field(description="Role level: Internship, Fresher, Entry-level, Junior, etc.")
    is_internship: Optional[bool] = Field(description="True if internship")
    is_fresher: Optional[bool] = Field(description="True if suitable for freshers/new grads")
    graduation_years: List[int] = Field(default_factory=list, description="Graduation years mentioned (e.g., 2026)")
    internship_duration: Optional[str] = Field(description="Internship duration if any")
    stipend_range: Optional[str] = Field(description="Stipend range if internship")
    application_deadline: Optional[str] = Field(description="Deadline date/phrase if mentioned")
    eligibility_criteria: Optional[str] = Field(description="Key eligibility constraints")

class LinkedInLeadExtractor:
    """Main class for extracting leads from LinkedIn posts using Gemini"""

    def __init__(self, gemini_keys_path: str = "gemini_keys.json", model_name: str = "gemini-2.5-flash",
                 preferred_roles: List[str] = None, preferred_locations: List[str] = None,
                 strict_filtering: bool = True, config_manager: ConfigManager = None, preference_manager: PreferenceManager = None):

        self.config_manager = config_manager or ConfigManager()
        self.key_manager = KeyManager(gemini_keys_path, "gemini")
        self.preferences = PreferenceManager("/home/Lazycat/mysite/configs/preferences.json")

        # Get available keys from key manager
        available_keys = self.key_manager.get_available_keys()

        if not available_keys:
            raise ValueError("No Gemini API keys available")

        # Create multiple LLM instances for parallel processing
        self.llms = []
        for api_key in available_keys:
            if not api_key:
                continue
            try:
                llm = ChatGoogleGenerativeAI(
                    model=model_name,
                    google_api_key=api_key,
                    temperature=0.1
                )
                self.llms.append(llm)
                logging.info(f"Initialized LLM with key: {api_key[:10]}...")
            except Exception as e:
                logging.warning(f"Failed to initialize LLM with key {api_key[:10]}...: {str(e)}")

        if not self.llms:
            raise ValueError("No valid API keys provided")

        self.current_llm_index = 0  # For round-robin API key usage
        self.parser = PydanticOutputParser(pydantic_object=LeadExtractionOutput)

        self.preferred_roles = self.preferences.preferred_roles()
        self.preferred_locations = self.preferences.preferred_locations()
        self.strict_filtering = strict_filtering
        self.extraction_prompt = self._create_extraction_prompt()
        self.filtering_prompt = self._create_filtering_prompt()

        logging.info(f"LinkedInLeadExtractor initialized with {len(self.llms)} LLM instances")

    def _get_next_llm(self) -> ChatGoogleGenerativeAI:
        """Get next available LLM instance in round-robin fashion"""
        llm = self.llms[self.current_llm_index]
        self.current_llm_index = (self.current_llm_index + 1) % len(self.llms)
        return llm

    def _create_filtering_prompt(self) -> ChatPromptTemplate:
        """Create prompt for initial filtering based on user preferences"""
        filtering_template = """
        You are an expert at analyzing LinkedIn posts to determine if they match specific job preferences.

        ANALYZE this LinkedIn post and determine if it matches the user's preferences:

        POST TEXT: {post_text}
        AUTHOR: {author_name}
        AUTHOR HEADLINE: {author_headline}

        USER'S PREFERRED ROLES: {preferred_roles}
        USER'S PREFERRED LOCATIONS: {preferred_locations}

        TASK: Determine if this post should be processed for lead extraction.

        STRICT MATCHING CRITERIA (ALL MUST BE MET):

        1. JOB ROLE MATCHING (MANDATORY):
           - MUST be a pure software development or AI/ML engineering role
           - Look for EXACT role matches or closely related technical roles
           - ACCEPTED ROLES:
             * Software Developer/Software Engineer/Python Developer
             * Backend Developer/Frontend Developer/Full Stack Developer
             * AI Engineer/ML Engineer/Data Scientist/Data Engineer
             * DevOps Engineer/Cloud Engineer/SRE (if development-focused)

           - REJECTED ROLES:
             * Non-technical roles (Sales, Marketing, HR, Business Analyst)
             * Pure management roles (unless technical manager)
             * Support/QA roles (unless development-focused)
             * Internship coordinator/recruiter roles
        3. Banned companies :
            * TEN
            * Prodigy
            * Bharat Intern
            * GAO Group

            If in post there is any text like  AICTE, MSME certified reject the post.


        2. LOCATION MATCHING (MANDATORY):
           - MUST match preferred locations exactly
           - "Remote" matches if user prefers remote work
           - City/State/Country must match user preferences
           - If no location specified, consider as "flexible" but still check role match
           - Strictly REJECT if location explicitly doesn't match preferences

        3. POST TYPE VERIFICATION (MANDATORY):
           - MUST be a genuine job posting with hiring intent
           - Look for keywords: "hiring", "recruiting", "vacancy", "position", "role", "internship", "intern"
           - REJECT: Educational posts, general networking, company updates, non-hiring content

        4. SENIORITY FILTER (CRITICAL - MANDATORY):
           - ONLY match Internship or Fresher/Entry-level roles
           - Strictly follow: 2026 batch/passout/graduating students
           - ACCEPTED PHRASES: "intern", "internship", "fresher", "0-1 years", "new grad", "2026", "batch", "passout", "entry level"
           - REJECT: Mid/senior roles (senior, lead, 3+ years, architect, principal) unless explicitly includes freshers/interns
           - REJECT: Experience requirements > 1 year unless internship

        5. TECHNICAL REQUIREMENTS CHECK:
           - Should mention relevant technologies (Python, Java, JavaScript, AI/ML tools)
           - REJECT if purely non-technical requirements

        6. Reject all unpaid

        TEMPLATE LABELING (if all criteria met):
        - "MATCH_1" = Software Developer roles (backend, frontend, fullstack, web dev)
        - "MATCH_2" = AI/ML/Data Science roles (ML engineer, data scientist, AI developer)

        RESPONSE FORMAT:
        Return ONLY one of these responses:
        - "MATCH_1" - matches Software Developer criteria
        - "MATCH_2" - matches AI/ML/Data criteria
        - "NO_MATCH_ROLE" - job role doesn't match technical preferences
        - "NO_MATCH_LOCATION" - location doesn't match preferences
        - "NO_MATCH_NOT_JOB" - not a genuine job posting
        - "NO_MATCH_SENIORITY" - not internship/fresher level
        - "NO_MATCH_INSUFFICIENT_INFO" - insufficient information to determine

        BE EXTREMELY STRICT: Only accept posts that clearly meet ALL criteria.
        """
        return ChatPromptTemplate.from_template(filtering_template)

    def _create_extraction_prompt(self) -> ChatPromptTemplate:
        """Create the prompt template for entity extraction"""
        prompt_template = """
        You are an expert at extracting structured information from LinkedIn posts for lead generation.

        Analyze the following LinkedIn post and extract relevant information:

        POST TEXT: {post_text}
        AUTHOR: {author_name}
        AUTHOR HEADLINE: {author_headline}

        Extract the following information with HIGH PRECISION:

        1. JOB DETAILS (EXTRACT EXACTLY):
           - Job title (if multiple roles mentioned, select ONLY the one that best matches user preferences: Software Developer, AI Engineer, ML Engineer, Data Scientist, Python Developer, Java Developer, Full Stack Developer, Backend Developer, Frontend Developer)
           - Company name (look for company mentions, client references, employer)
           - Location (extract exact location: city, state, country, or "Remote")
           - Work mode (Remote, Hybrid, On-site, or not specified)
           - Experience level (Fresher, Entry-level, Junior, Intern, Senior, etc.)
           - Salary/Stipend range (extract exact amounts or ranges mentioned)

        2. TECHNICAL REQUIREMENTS (BE SPECIFIC):
           - Tech stack (extract specific technologies: Python, Java, SQL, React, TensorFlow, etc.)
           - Skills required (extract skills explicitly mentioned as requirements)
           - Programming languages mentioned
           - Frameworks and tools mentioned

        3. APPLICATION METHOD (DETERMINE PRECISELY):
           - Determine how to apply:
             * "email" if email address is provided
             * "link" if application link/URL is provided
             * "dm" if they ask to DM/message them
             * "other" for other methods

           - Extract the actual email address or link
           - Be precise: only mark as "email" if actual email is provided


        4. POST CLASSIFICATION (VERIFY CAREFULLY):
           - Is this a job posting? (True/False)
           - Post category: "job", "educational", "networking", "other"
           - Verify it's actually hiring/recruiting, not just sharing information

        5. INTERNSHIP/FRESHER INSIGHTS (CRITICAL ACCURACY):
           - Role level (Internship, Fresher, Entry-level, Junior, Senior)
           - Is internship? True/False (only if explicitly mentioned)
           - Is fresher-friendly? True/False (only if explicitly mentioned)
           - Graduation years mentioned (extract exact years like 2026)
           - Internship duration (extract exact duration: 3 months, 6 months, etc.)
           - Stipend (extract exact amounts if mentioned)
           - Application deadline (extract exact dates or phrases)
           - Eligibility criteria (extract specific requirements: batch, degree, CGPA)

        EXTRACTION RULES:
        - Extract ONLY information explicitly mentioned in the post
        - Do NOT infer or assume information not present
        - For email addresses, use exact format found (xxx@domain.com)
        - For URLs, extract complete links
        - For tech stack, list only technologies explicitly mentioned
        - For location, extract exact location mentioned
        - Return "not specified" if information is not available
        - Be precise with job titles and company names
        - Extract graduation years exactly as mentioned (e.g., 2026)

        ROLE SELECTION PRIORITY (if multiple roles mentioned):
        1. Software Developer/Software Engineer/Python Developer/Java Developer
        2. AI Engineer/ML Engineer/Data Scientist/Data Engineer
        3. Full Stack Developer/Backend Developer/Frontend Developer
        4. DevOps Engineer/Cloud Engineer (if development-focused)
        5. Mobile Developer/React Developer/Node.js Developer

        If multiple roles are mentioned, select ONLY the highest priority role that matches user preferences.

        {format_instructions}
        """
        return ChatPromptTemplate.from_template(prompt_template)

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
        """Select the most relevant role from multiple roles mentioned"""
        if not job_title or not preferred_roles:
            return job_title

        job_title_lower = job_title.lower()

        # Priority order based on user preferences
        role_priority = []
        for role in preferred_roles:
            role_lower = role.lower()
            if role_lower in job_title_lower:
                role_priority.append(role)

        # If no exact matches, try partial matches
        if not role_priority:
            for role in preferred_roles:
                role_lower = role.lower()
                # Check for key terms in the role
                if 'software' in role_lower and ('developer' in job_title_lower or 'engineer' in job_title_lower):
                    role_priority.append(role)
                elif 'ai' in role_lower and ('ai' in job_title_lower or 'ml' in job_title_lower or 'data' in job_title_lower):
                    role_priority.append(role)
                elif 'python' in role_lower and 'python' in job_title_lower:
                    role_priority.append(role)
                elif 'java' in role_lower and 'java' in job_title_lower:
                    role_priority.append(role)

        # Return the first match, or original if no matches
        return role_priority[0] if role_priority else job_title

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

    def remove_duplicates(self, leads: List[ExtractedLead]) -> Tuple[List[ExtractedLead], Dict[str, int]]:
        """Remove duplicate entries based on company and role combination"""
        unique_leads = []
        seen_keys = set()
        duplicate_stats = {
            'total_leads': len(leads),
            'unique_leads': 0,
            'duplicates_removed': 0,
            'duplicate_details': []
        }

        for lead in leads:
            duplicate_key = self._generate_duplicate_key(lead.company_name, lead.job_title)
            lead.duplicate_key = duplicate_key

            if duplicate_key not in seen_keys:
                if not lead.email_template_type:
                    lead.email_template_type = self._determine_email_template_type(lead)
                # Coerce to supported set {software_dev, ai}
                if lead.email_template_type not in {'software_dev', 'ai'}:
                    lead.email_template_type = 'software_dev'
                unique_leads.append(lead)
                seen_keys.add(duplicate_key)
                duplicate_stats['unique_leads'] += 1
            else:
                duplicate_stats['duplicates_removed'] += 1
                duplicate_stats['duplicate_details'].append({
                    'company': lead.company_name,
                    'job_title': lead.job_title,
                    'author': lead.author_name,
                    'duplicate_key': duplicate_key
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
            response = llm.invoke([HumanMessage(content=formatted_prompt)])
            result = response.content.strip()
            is_match = result.startswith("MATCH_")
            return is_match, result
        except Exception as e:
            print(f"Error in filtering post {post_data.get('urn', 'unknown')[:20]}: {e}")
            # Try another LLM if available
            for _ in range(len(self.llms) - 1):
                try:
                    llm = self._get_next_llm()
                    response = llm.invoke([HumanMessage(content=formatted_prompt)])
                    result = response.content.strip()
                    is_match = result.startswith("MATCH_")
                    return is_match, result
                except Exception:
                    continue
            return True, "FILTERING_ERROR"

    def process_single_post(self, post_data: Dict) -> Optional[ExtractedLead]:
        """Process a single LinkedIn post and extract lead information"""
        if not post_data.get('text'):
            print(f"Skipping post {post_data.get('urn', 'unknown')[:20]}: No text content")
            return None

        should_process, filter_reason = self.should_process_post(post_data)
        if not should_process:
            print(f"Skipping post {post_data.get('urn', 'unknown')[:20]}... - Reason: {filter_reason}")
            return None



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
            response = llm.invoke([HumanMessage(content=formatted_prompt)])
            extracted_data = self.parser.parse(response.content)
        except Exception as e:
            print(f"Error processing post {post_data.get('urn', 'unknown')}: {e}")
            # Try another LLM if available
            for _ in range(len(self.llms) - 1):
                try:
                    llm = self._get_next_llm()
                    response = llm.invoke([HumanMessage(content=formatted_prompt)])
                    extracted_data = self.parser.parse(response.content)
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

        # Map filtering label to template type: MATCH_1 -> software_dev, MATCH_2 -> ai
        template_type = None
        if isinstance(filter_reason, str) and filter_reason.startswith("MATCH_"):
            if filter_reason.endswith("1"):
                template_type = 'software_dev'
            elif filter_reason.endswith("2"):
                template_type = 'ai'

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
        return ExtractedLead(
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
            eligibility_criteria=getattr(extracted_data, 'eligibility_criteria', None) if hasattr(extracted_data, 'eligibility_criteria') else None
        )

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

        return LeadExtractionOutput(
            job_title=post_data.get('title'),
            company_name=None,
            location=None,
            work_mode=None,
            experience_level=None,
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
                           start_idx: int, total_posts: int) -> Tuple[List[ExtractedLead], Dict[str, int]]:
        """Process a subset of posts using a specific LLM instance"""
        extracted_leads = []
        filtering_stats = {
            'processed': 0,
            'skipped_role': 0,
            'skipped_location': 0,
            'skipped_not_job': 0,
            'skipped_insufficient_info': 0,
            'skipped_seniority': 0,
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

                lead = self.process_single_post(post)

                # Restore original method
                self._get_next_llm = original_get_next_llm

                if lead:
                    extracted_leads.append(lead)
                    filtering_stats['processed'] += 1
                else:
                    _, reason = self.should_process_post(post)
                    if reason == "NO_MATCH_ROLE":
                        filtering_stats['skipped_role'] += 1
                    elif reason == "NO_MATCH_LOCATION":
                        filtering_stats['skipped_location'] += 1
                    elif reason == "NO_MATCH_NOT_JOB":
                        filtering_stats['skipped_not_job'] += 1
                    elif reason == "NO_MATCH_INSUFFICIENT_INFO":
                        filtering_stats['skipped_insufficient_info'] += 1
                    elif reason == "FILTERING_ERROR":
                        filtering_stats['filtering_errors'] += 1
            except Exception as e:
                print(f"Error processing post {global_idx + 1}: {e}")
                filtering_stats['extraction_errors'] += 1

        return extracted_leads, filtering_stats

    def process_posts_batch(self, posts_data: List[Dict]) -> Tuple[List[ExtractedLead], Dict[str, int]]:
        """Process a batch of LinkedIn posts in parallel using multiple API keys with dynamic chunking"""
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
            'filtering_errors': 0,
            'extraction_errors': 0
        }

        logging.info(f"Starting parallel processing with {num_workers} workers")
        logging.info(f"Optimal chunk size: {optimal_chunk_size} posts per worker")
        logging.info(f"Total chunks: {len(post_chunks)}")

        if not self.config_manager.get("enable_parallel_extraction", True):
            logging.info("Parallel extraction disabled, processing sequentially")
            for chunk, start_idx in post_chunks:
                chunk_leads, chunk_stats = self.process_posts_subset(chunk, self.llms[0], start_idx, total_posts)
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
                        total_posts
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
        logging.info(f"Filtering Errors: {combined_stats['filtering_errors']}")
        logging.info(f"Extraction Errors: {combined_stats['extraction_errors']}")
        logging.info("="*50)

        return all_leads, combined_stats

    def filter_job_posts(self, leads: List[ExtractedLead]) -> List[ExtractedLead]:
        """Filter only job-related posts"""
        return [lead for lead in leads if lead.is_job_posting]


