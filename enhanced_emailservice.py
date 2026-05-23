import smtplib
import pandas as pd
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
import os
from typing import List, Dict, Optional, Tuple
from datetime import datetime
import logging

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class EmailService:
    """Enhanced email service for sending personalized emails with resume attachments"""
    
    def __init__(self, sender_email: str, sender_password: str, smtp_server: str = 'smtp.gmail.com', smtp_port: int = 587):
        self.sender_email = sender_email
        self.sender_password = sender_password
        self.smtp_server = smtp_server
        self.smtp_port = smtp_port
        self.resume_folder = "resumes"
        
    def _get_resume_path(self, template_type: str) -> Optional[str]:
        """Get the appropriate resume path based on template type"""
        resume_mapping = {
            'software_dev': '/home/Lazycat/mysite/resumes/software_dev_resume.pdf',
            'ai': '/home/Lazycat/mysite/resumes/ai_ml_resume.pdf'
        }
        
        filename = resume_mapping.get(template_type)
        if not filename:
            return None
            
        resume_path = filename
        logger.warning(f"Resume path is {resume_path}")
        print(f"Resume path is {resume_path}")
        return resume_path 
        
        
    def send_single_email(self, to_email: str, subject: str, body: str, template_type: str = 'software_dev') -> bool:
        """Send a single email with resume attachment"""
        try:
            # Create message
            msg = MIMEMultipart()
            msg['From'] = self.sender_email
            msg['To'] = to_email
            msg['Subject'] = subject
            
            # Add body
            msg.attach(MIMEText(body, 'plain'))
            
            # Add resume attachment if available
            resume_path = self._get_resume_path(template_type)
            if resume_path:
                with open(resume_path, 'rb') as attachment:
                    part = MIMEBase('application', 'octet-stream')
                    part.set_payload(attachment.read())
                encoders.encode_base64(part)
                part.add_header(
                    'Content-Disposition',
                    f'attachment; filename={os.path.basename(resume_path)}'
                )
                msg.attach(part)
                logger.info(f"Attached resume: {os.path.basename(resume_path)}")
            else:
                logger.warning(f"No resume found for template type: {template_type}, Resume path : {resume_path}")
            
            # Send email
            server = smtplib.SMTP(self.smtp_server, self.smtp_port)
            server.starttls()
            server.login(self.sender_email, self.sender_password)
            server.sendmail(self.sender_email, to_email, msg.as_string())
            server.quit()
            
            logger.info(f"Email sent successfully to {to_email}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to send email to {to_email}: {e}")
            return False
    
    def send_batch_emails(self, email_data: List[Dict]) -> Tuple[int, int]:
        """Send multiple emails with individual personalization"""
        sent_count = 0
        failed_count = 0
        
        for email_info in email_data:
            to_email = email_info.get('to_email')
            subject = email_info.get('subject')
            body = email_info.get('body')
            template_type = email_info.get('template_type', 'software_dev')
            
            if not all([to_email, subject, body]):
                logger.error(f"Missing required email data: {email_info}")
                failed_count += 1
                continue
            
            success = self.send_single_email(to_email, subject, body, template_type)
            if success:
                sent_count += 1
            else:
                failed_count += 1
        
        logger.info(f"Batch email complete: {sent_count} sent, {failed_count} failed")
        return sent_count, failed_count
    
    def send_emails_from_csv(self, csv_file: str, subject: str, email_body: str, attachment_path: Optional[str] = None) -> Tuple[int, int]:
        """Legacy method for backward compatibility"""
        try:
            data = pd.read_csv(csv_file)
            email_column = 'email'
            email_list = data[email_column].dropna().tolist()
        except Exception as e:
            logger.error(f"Error reading CSV: {e}")
            return 0, 0

        if attachment_path and not os.path.exists(attachment_path):
            logger.error("Attachment file not found!")
            return 0, 0

        sent_count = 0
        failed_count = 0
        
        try:
            server = smtplib.SMTP(self.smtp_server, self.smtp_port)
            server.starttls()
            server.login(self.sender_email, self.sender_password)
        except Exception as e:
            logger.error(f"Error connecting to SMTP server: {e}")
            return 0, 0

        # Send emails one by one
        for recipient in email_list:
            try:
                msg = MIMEMultipart()
                msg['From'] = self.sender_email
                msg['To'] = recipient
                msg['Subject'] = subject

                msg.attach(MIMEText(email_body, 'plain'))

                if attachment_path:
                    with open(attachment_path, 'rb') as attachment:
                        part = MIMEBase('application', 'octet-stream')
                        part.set_payload(attachment.read())
                    encoders.encode_base64(part)
                    part.add_header(
                        'Content-Disposition',
                        f'attachment; filename={os.path.basename(attachment_path)}'
                    )
                    msg.attach(part)

                server.sendmail(self.sender_email, recipient, msg.as_string())
                logger.info(f"Email sent to {recipient}")
                sent_count += 1
            except Exception as e:
                logger.error(f"Failed to send email to {recipient}: {e}")
                failed_count += 1

        server.quit()
        logger.info(f"All emails processed: {sent_count} sent, {failed_count} failed")
        return sent_count, failed_count

def create_email_service_from_env() -> Optional[EmailService]:
    """Create EmailService instance from environment variables"""
    sender_email = "ymmmahamulkar@gmail.com"
    sender_password = "besg unwh pryx oqkd"
    
    if not sender_email or not sender_password:
        logger.error("SENDER_EMAIL and SENDER_PASSWORD environment variables are required")
        return None
    
    return EmailService(sender_email, sender_password)

if __name__ == "__main__":
    # Example usage
    email_service = create_email_service_from_env()
    if email_service:
        # Test single email
        success = email_service.send_single_email(
            to_email="ymmahamulkar@gmail.com",
            subject="Test Email",
            body="This is a test email with resume attachment.",
            template_type="software_dev"
        )
        print(f"Email sent: {success}")
