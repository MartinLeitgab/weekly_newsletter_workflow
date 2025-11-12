#!/usr/bin/env python3
"""
AI Safety Weekly Digest Generator
Fetches papers from Slack, Gmail newsletters, and generates a comprehensive summary using Claude API
"""

import os
import re
import json
import base64
import requests
from datetime import datetime, timedelta
from typing import List, Dict, Set
import anthropic
from slack_sdk import WebClient
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import smtplib


class ContentFetcher:
    """Handles fetching content from various sources"""
    
    def __init__(self):
        self.slack_token = os.environ.get('SLACK_BOT_TOKEN')
        self.gmail_creds = self._setup_gmail_credentials()
        self.anthropic_client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))
        
    def _setup_gmail_credentials(self):
        """Setup Gmail API credentials from environment variables"""
        creds_json = os.environ.get('GMAIL_CREDENTIALS_JSON')
        if creds_json:
            creds_data = json.loads(creds_json)
            return Credentials.from_authorized_user_info(creds_data)
        return None
    
    def fetch_slack_urls(self, channel_name: str, days_back: int = 7) -> List[str]:
        """Fetch all URLs from Slack channel from the last N days"""
        client = WebClient(token=self.slack_token)
        
        # Get channel ID
        channels = client.conversations_list()
        channel_id = None
        for channel in channels['channels']:
            if channel['name'] == channel_name:
                channel_id = channel['id']
                break
        
        if not channel_id:
            print(f"Channel {channel_name} not found")
            return []
        
        # Calculate timestamp for N days ago
        oldest = (datetime.now() - timedelta(days=days_back)).timestamp()
        
        # Fetch messages
        result = client.conversations_history(
            channel=channel_id,
            oldest=str(oldest)
        )
        
        # Extract all URLs using regex
        url_pattern = r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+'
        urls = []
        
        for message in result['messages']:
            text = message.get('text', '')
            found_urls = re.findall(url_pattern, text)
            urls.extend(found_urls)
        
        # Remove duplicates while preserving order
        seen = set()
        unique_urls = []
        for url in urls:
            if url not in seen:
                seen.add(url)
                unique_urls.append(url)
        
        print(f"Found {len(unique_urls)} unique URLs from Slack")
        return unique_urls
    
    def fetch_gmail_newsletters(self, label_name: str, days_back: int = 7) -> List[Dict]:
        """Fetch emails with specific label from Gmail"""
        if not self.gmail_creds:
            print("Gmail credentials not configured")
            return []
        
        service = build('gmail', 'v1', credentials=self.gmail_creds)
        
        # Calculate date for query
        date_filter = (datetime.now() - timedelta(days=days_back)).strftime('%Y/%m/%d')
        
        # Search for emails with label
        query = f'label:{label_name} after:{date_filter}'
        results = service.users().messages().list(userId='me', q=query).execute()
        messages = results.get('messages', [])
        
        newsletters = []
        for msg in messages:
            full_msg = service.users().messages().get(userId='me', id=msg['id'], format='full').execute()
            
            # Extract subject
            subject = ''
            for header in full_msg['payload']['headers']:
                if header['name'] == 'Subject':
                    subject = header['value']
                    break
            
            # Extract body
            body = self._get_email_body(full_msg['payload'])
            
            newsletters.append({
                'subject': subject,
                'body': body,
                'id': msg['id']
            })
        
        print(f"Found {len(newsletters)} newsletters from Gmail")
        return newsletters
    
    def _get_email_body(self, payload):
        """Extract email body from Gmail message payload"""
        if 'parts' in payload:
            for part in payload['parts']:
                if part['mimeType'] == 'text/plain':
                    data = part['body'].get('data', '')
                    if data:
                        return base64.urlsafe_b64decode(data).decode('utf-8')
                elif part['mimeType'] == 'text/html':
                    data = part['body'].get('data', '')
                    if data:
                        return base64.urlsafe_b64decode(data).decode('utf-8')
        else:
            data = payload['body'].get('data', '')
            if data:
                return base64.urlsafe_b64decode(data).decode('utf-8')
        return ''
    
    def download_paper_content(self, url: str) -> Dict:
        """Download and extract content from a paper URL"""
        print(f"Processing URL: {url}")
        
        # Handle arXiv URLs - convert to PDF
        if 'arxiv.org' in url:
            if '/abs/' in url:
                pdf_url = url.replace('/abs/', '/pdf/') + '.pdf'
            elif '/pdf/' in url:
                pdf_url = url if url.endswith('.pdf') else url + '.pdf'
            else:
                pdf_url = url
            
            return self._download_pdf(pdf_url, url)
        
        # Handle direct PDF links
        elif url.endswith('.pdf'):
            return self._download_pdf(url, url)
        
        # Handle web pages (OpenReview, blogs, etc.)
        else:
            return self._fetch_webpage(url)
    
    def _download_pdf(self, pdf_url: str, original_url: str) -> Dict:
        """Download PDF and convert to text"""
        try:
            response = requests.get(pdf_url, timeout=30)
            response.raise_for_status()
            
            # Save PDF temporarily
            pdf_path = '/tmp/temp_paper.pdf'
            with open(pdf_path, 'wb') as f:
                f.write(response.content)
            
            # Convert PDF to text using pdfplumber
            import pdfplumber
            text = ""
            with pdfplumber.open(pdf_path) as pdf:
                for page in pdf.pages:
                    text += page.extract_text() or ""
            
            # Clean up
            os.remove(pdf_path)
            
            return {
                'url': original_url,
                'type': 'pdf',
                'content': text[:100000],  # Limit to ~100k chars
                'success': True
            }
        except Exception as e:
            print(f"Error downloading PDF {pdf_url}: {e}")
            return {
                'url': original_url,
                'type': 'pdf',
                'content': '',
                'success': False,
                'error': str(e)
            }
    
    def _fetch_webpage(self, url: str) -> Dict:
        """Fetch content from a webpage"""
        try:
            response = requests.get(url, timeout=30, headers={'User-Agent': 'Mozilla/5.0'})
            response.raise_for_status()
            
            # Basic HTML cleaning
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # Remove script and style elements
            for script in soup(["script", "style"]):
                script.decompose()
            
            # Get text
            text = soup.get_text()
            
            # Clean up whitespace
            lines = (line.strip() for line in text.splitlines())
            chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
            text = '\n'.join(chunk for chunk in chunks if chunk)
            
            return {
                'url': url,
                'type': 'webpage',
                'content': text[:50000],  # Limit content
                'success': True
            }
        except Exception as e:
            print(f"Error fetching webpage {url}: {e}")
            return {
                'url': url,
                'type': 'webpage',
                'content': '',
                'success': False,
                'error': str(e)
            }


class DigestGenerator:
    """Generates the weekly digest using Claude API"""
    
    def __init__(self, anthropic_client):
        self.client = anthropic_client
    
    def generate_digest(self, papers: List[Dict], newsletters: List[Dict]) -> str:
        """Generate comprehensive weekly digest using Claude"""
        
        # Prepare content for Claude
        content_parts = []
        
        # Add newsletters
        if newsletters:
            content_parts.append("=== EMAIL NEWSLETTERS ===\n")
            for i, newsletter in enumerate(newsletters, 1):
                content_parts.append(f"\n--- Newsletter {i}: {newsletter['subject']} ---\n")
                content_parts.append(newsletter['body'][:10000])  # Limit each newsletter
        
        # Add papers
        if papers:
            content_parts.append("\n\n=== PAPERS FROM SLACK ===\n")
            for i, paper in enumerate(papers, 1):
                if paper['success']:
                    content_parts.append(f"\n--- Paper {i}: {paper['url']} ---\n")
                    content_parts.append(f"Type: {paper['type']}\n")
                    content_parts.append(paper['content'][:30000])  # Limit each paper
                else:
                    content_parts.append(f"\n--- Paper {i}: {paper['url']} (failed to fetch) ---\n")
        
        full_content = '\n'.join(content_parts)
        
        # Create prompt for Claude
        prompt = f"""You are analyzing this week's AI safety content from newsletters and research papers. Your task is to create a COMPACT weekly digest that:

1. Removes all redundancy (many sources cover the same topics/papers)
2. Extracts the most important insights, findings, and developments
3. Highlights key data, results, or plots mentioned
4. Organizes information by theme/topic rather than by source
5. Keeps the summary concise but comprehensive

Focus on:
- Novel research findings in AI safety/alignment
- Important policy or governance developments
- Technical breakthroughs or concerning capabilities
- Key empirical results and data
- Significant debates or discussions in the field

Format the output with clear sections and bullet points for scannability.

Here is this week's content:

{full_content}

Generate the compact weekly digest now:"""

        print("Sending content to Claude for analysis...")
        
        # Send to Claude API with extended thinking
        message = self.client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=16000,
            thinking={
                "type": "enabled",
                "budget_tokens": 10000
            },
            messages=[{
                "role": "user",
                "content": prompt
            }]
        )
        
        # Extract the text response (skip thinking blocks)
        digest_text = ""
        for block in message.content:
            if block.type == "text":
                digest_text += block.text
        
        return digest_text


class EmailSender:
    """Sends the digest via email"""
    
    def __init__(self):
        self.smtp_server = os.environ.get('SMTP_SERVER', 'smtp.gmail.com')
        self.smtp_port = int(os.environ.get('SMTP_PORT', '587'))
        self.sender_email = os.environ.get('SENDER_EMAIL')
        self.sender_password = os.environ.get('SENDER_PASSWORD')
        self.recipient_email = os.environ.get('RECIPIENT_EMAIL')
    
    def send_digest(self, digest_content: str):
        """Send the digest via email"""
        
        # Create message
        msg = MIMEMultipart('alternative')
        msg['Subject'] = f"AI Safety Weekly Digest - {datetime.now().strftime('%Y-%m-%d')}"
        msg['From'] = self.sender_email
        msg['To'] = self.recipient_email
        
        # Add plain text and HTML versions
        text_part = MIMEText(digest_content, 'plain')
        msg.attach(text_part)
        
        # Convert to simple HTML
        html_content = digest_content.replace('\n', '<br>')
        html_part = MIMEText(f'<html><body><pre style="font-family: sans-serif;">{html_content}</pre></body></html>', 'html')
        msg.attach(html_part)
        
        # Send email
        try:
            with smtplib.SMTP(self.smtp_server, self.smtp_port) as server:
                server.starttls()
                server.login(self.sender_email, self.sender_password)
                server.send_message(msg)
            print(f"Digest sent successfully to {self.recipient_email}")
        except Exception as e:
            print(f"Error sending email: {e}")
            raise


def main():
    """Main execution function"""
    
    print("=== AI Safety Weekly Digest Generator ===")
    print(f"Started at: {datetime.now()}")
    
    # Configuration from environment
    slack_channel = os.environ.get('SLACK_CHANNEL_NAME', 'papers-running-list')
    gmail_label = os.environ.get('GMAIL_LABEL', 'AI-Safety-Newsletters')
    days_back = int(os.environ.get('DAYS_BACK', '7'))
    
    # Initialize components
    fetcher = ContentFetcher()
    
    # Fetch content from sources
    print("\n1. Fetching URLs from Slack...")
    slack_urls = fetcher.fetch_slack_urls(slack_channel, days_back)
    
    print("\n2. Fetching newsletters from Gmail...")
    newsletters = fetcher.fetch_gmail_newsletters(gmail_label, days_back)
    
    print("\n3. Downloading paper content...")
    papers = []
    for url in slack_urls[:50]:  # Limit to 50 papers to avoid overwhelming
        paper_content = fetcher.download_paper_content(url)
        papers.append(paper_content)
    
    print(f"\n4. Successfully processed {sum(1 for p in papers if p['success'])} papers")
    
    # Generate digest
    print("\n5. Generating digest with Claude...")
    generator = DigestGenerator(fetcher.anthropic_client)
    digest = generator.generate_digest(papers, newsletters)
    
    # Save digest locally for debugging
    with open('/tmp/digest.txt', 'w') as f:
        f.write(digest)
    print("Digest saved to /tmp/digest.txt")
    
    # Send email
    print("\n6. Sending digest via email...")
    sender = EmailSender()
    sender.send_digest(digest)
    
    print("\n=== Completed successfully ===")
    print(f"Finished at: {datetime.now()}")


if __name__ == "__main__":
    main()
