import os
import sys
import json
import base64
import time
import logging
from datetime import datetime
from typing import List, Dict, Any, Optional
from pathlib import Path

try:
    import requests
    from bs4 import BeautifulSoup
    import backoff
except ImportError as e:
    print(f"Missing required package: {e}", file=sys.stderr)
    sys.exit(1)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('logs/bug_fetcher.log', mode='a')
    ]
)
logger = logging.getLogger(__name__)

class ADOBugFetcher:
    """Production-ready Azure DevOps bug fetcher with comprehensive error handling."""
    
    def __init__(self):
        self.bug_id = os.getenv("ADO_BUG_ID")
        self.org_url = os.getenv("ADO_ORG_URL")
        self.project_name = os.getenv("ADO_PROJECT_NAME")
        self.pat = os.getenv("ADO_PAT")
        
        # Create cache directory if it doesn't exist
        self.cache_dir = Path("cache")
        self.cache_dir.mkdir(exist_ok=True)
        
        # Create logs directory if it doesn't exist
        logs_dir = Path("logs")
        logs_dir.mkdir(exist_ok=True)
        
        # Validation
        self._validate_environment()
        
        # Request session with retry configuration
        self.session = requests.Session()
        self.session.headers.update(self._auth_header())
        
        logger.info(f"Initialized fetcher for bug {self.bug_id}")

    def _validate_environment(self) -> None:
        """Validate required environment variables."""
        missing = []
        if not self.bug_id:
            missing.append("ADO_BUG_ID")
        if not self.org_url:
            missing.append("ADO_ORG_URL")
        if not self.project_name:
            missing.append("ADO_PROJECT_NAME")
        if not self.pat:
            missing.append("ADO_PAT")
            
        if missing:
            logger.error(f"Missing required environment variables: {', '.join(missing)}")
            raise ValueError(f"Missing required environment variables: {missing}")
            
        # Validate bug ID format
        if not self.bug_id.isdigit():
            logger.error(f"Invalid bug ID format: {self.bug_id}")
            raise ValueError(f"Invalid bug ID format: {self.bug_id}")

    def _auth_header(self) -> Dict[str, str]:
        """Generate authentication header for Azure DevOps API."""
        token = base64.b64encode(f":{self.pat}".encode()).decode()
        return {
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

    @staticmethod
    def _is_image(url: str) -> bool:
        """Check if URL points to an image file."""
        return url.lower().endswith((".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".svg"))

    @staticmethod
    def _html_to_text(html: str) -> Optional[str]:
        """Convert HTML to clean text using BeautifulSoup."""
        if not html:
            return None
        try:
            soup = BeautifulSoup(html, "html.parser")
            # Remove script and style elements
            for script in soup(["script", "style"]):
                script.decompose()
            return soup.get_text(separator="\n", strip=True)
        except Exception as e:
            logger.warning(f"HTML parsing error: {e}")
            return html  # Return raw HTML if parsing fails

    def _flatten_comments(self, comments_obj: Dict[str, Any]) -> List[str]:
        """Extract and clean comments from ADO response."""
        try:
            comments = comments_obj.get("comments", [])
            if isinstance(comments, list):
                return [self._html_to_text(c.get("text", "")) for c in comments if c.get("text")]
            return []
        except Exception as e:
            logger.warning(f"Error processing comments: {e}")
            return []

    @backoff.on_exception(
        backoff.expo,
        (requests.exceptions.RequestException, requests.exceptions.Timeout),
        max_tries=3,
        max_time=30
    )
    def _safe_api_call(self, url: str, label: str) -> Dict[str, Any]:
        """Make API call with retry logic and comprehensive error handling."""
        try:
            logger.debug(f"Fetching {label}: {url}")
            
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
            
            # Validate JSON response
            data = response.json()
            logger.debug(f"Successfully fetched {label}")
            return data
            
        except requests.exceptions.Timeout:
            logger.error(f"Timeout fetching {label}: {url}")
            raise
        except requests.exceptions.HTTPError as e:
            logger.error(f"HTTP error fetching {label}: {e.response.status_code} - {e.response.text}")
            raise
        except requests.exceptions.RequestException as e:
            logger.error(f"Request error fetching {label}: {e}")
            raise
        except json.JSONDecodeError as e:
            logger.error(f"JSON decode error for {label}: {e}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error fetching {label}: {e}")
            raise

    def _fetch_work_item(self) -> Dict[str, Any]:
        """Fetch main work item with relations."""
        url = (
            f"{self.org_url}/{self.project_name}/_apis/wit/workitems/{self.bug_id}"
            "?api-version=7.1&$expand=relations"
        )
        return self._safe_api_call(url, "main work item")

    def _fetch_comments(self, work_item_id: str) -> List[str]:
        """Fetch comments for a work item."""
        url = (
            f"{self.org_url}/{self.project_name}/_apis/wit/workItems/{work_item_id}/comments"
            "?api-version=7.1-preview.3"
        )
        try:
            comments_data = self._safe_api_call(url, f"comments for {work_item_id}")
            return self._flatten_comments(comments_data)
        except Exception as e:
            logger.warning(f"Failed to fetch comments for {work_item_id}: {e}")
            return []

    def _process_relations(self, work_item: Dict[str, Any], bug_data: Dict[str, Any]) -> None:
        """Process work item relations (attachments, parent items)."""
        relations = work_item.get("relations", [])
        
        for relation in relations:
            rel_type = relation.get("rel")
            rel_url = relation.get("url", "")
            
            if rel_type == "AttachedFile":
                if self._is_image(rel_url):
                    bug_data["screenshots"].append({
                        "url": rel_url,
                        "name": os.path.basename(rel_url)
                    })
                else:
                    bug_data["attachments"].append({
                        "url": rel_url,
                        "name": os.path.basename(rel_url)
                    })
                    
            elif rel_type == "System.LinkTypes.Hierarchy-Reverse":
                # Process parent work item
                try:
                    parent_id = rel_url.split("/")[-1]
                    logger.info(f"Processing parent work item: {parent_id}")
                    
                    parent_url = (
                        f"{self.org_url}/{self.project_name}/_apis/wit/workitems/{parent_id}"
                        "?api-version=7.1&$expand=relations"
                    )
                    parent_wi = self._safe_api_call(parent_url, f"parent work item {parent_id}")
                    
                    # Extract parent details
                    p_fields = parent_wi.get("fields", {})
                    bug_data["parent"] = {
                        "id": parent_id,
                        "type": p_fields.get("System.WorkItemType"),
                        "title": p_fields.get("System.Title"),
                        "description": self._html_to_text(p_fields.get("System.Description")),
                        "repro_steps": self._html_to_text(p_fields.get("Microsoft.VSTS.TCM.ReproSteps")),
                        "state": p_fields.get("System.State"),
                        "assigned_to": p_fields.get("System.AssignedTo", {}).get("displayName"),
                        "attachments": [],
                        "screenshots": [],
                        "comments": self._fetch_comments(parent_id)
                    }
                    
                    # Process parent relations
                    for p_relation in parent_wi.get("relations", []):
                        if p_relation.get("rel") == "AttachedFile":
                            p_url = p_relation.get("url", "")
                            target_list = (bug_data["parent"]["screenshots"] 
                                         if self._is_image(p_url) 
                                         else bug_data["parent"]["attachments"])
                            target_list.append({
                                "url": p_url,
                                "name": os.path.basename(p_url)
                            })
                            
                except Exception as e:
                    logger.warning(f"Failed to process parent work item: {e}")
                    bug_data["parent"] = {"error": str(e)}

    def fetch_bug_details(self) -> Dict[str, Any]:
        """Main method to fetch and process bug details."""
        logger.info(f"Starting bug fetch for ID: {self.bug_id}")
        start_time = time.time()
        
        try:
            # Fetch main work item
            work_item = self._fetch_work_item()
            fields = work_item.get("fields", {})
            
            # Build base bug data structure
            bug_data = {
                "id": self.bug_id,
                "fetched_at": datetime.utcnow().isoformat() + "Z",
                "title": fields.get("System.Title"),
                "description": self._html_to_text(fields.get("System.Description")),
                "repro_steps": self._html_to_text(fields.get("Microsoft.VSTS.TCM.ReproSteps")),
                "state": fields.get("System.State"),
                "reason": fields.get("System.Reason"),
                "priority": fields.get("Microsoft.VSTS.Common.Priority"),
                "severity": fields.get("Microsoft.VSTS.Common.Severity"),
                "assigned_to": fields.get("System.AssignedTo", {}).get("displayName"),
                "created_by": fields.get("System.CreatedBy", {}).get("displayName"),
                "created_date": fields.get("System.CreatedDate"),
                "changed_date": fields.get("System.ChangedDate"),
                "system_info": fields.get("Custom.SystemInfo"),
                "area_path": fields.get("System.AreaPath"),
                "iteration_path": fields.get("System.IterationPath"),
                "custom_fields": {
                    "Module": fields.get("Custom.Module"),
                    "Feature": fields.get("Custom.Feature"),
                    "Build": fields.get("Custom.BuildVersion"),
                    "Environment": fields.get("Custom.Environment"),
                    "Browser": fields.get("Custom.Browser")
                },
                "attachments": [],
                "screenshots": [],
                "comments": [],
                "parent": {},
                "metadata": {
                    "work_item_type": fields.get("System.WorkItemType"),
                    "tags": fields.get("System.Tags", "").split(";") if fields.get("System.Tags") else []
                }
            }
            
            # Process relations (attachments, parent items)
            self._process_relations(work_item, bug_data)
            
            # Fetch comments for main work item
            bug_data["comments"] = self._fetch_comments(self.bug_id)
            
            # Add processing statistics
            processing_time = time.time() - start_time
            bug_data["processing_stats"] = {
                "processing_time_seconds": round(processing_time, 2),
                "attachments_count": len(bug_data["attachments"]),
                "screenshots_count": len(bug_data["screenshots"]),
                "comments_count": len(bug_data["comments"]),
                "has_parent": bool(bug_data["parent"])
            }
            
            logger.info(f"Bug fetch completed in {processing_time:.2f}s")
            return bug_data
            
        except Exception as e:
            logger.error(f"Failed to fetch bug details: {e}")
            raise

    def save_to_cache(self, bug_data: Dict[str, Any]) -> Path:
        """Save bug data to cache file."""
        cache_file = self.cache_dir / f"ado_bug_{self.bug_id}.json"
        
        try:
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(bug_data, f, indent=2, ensure_ascii=False)
            
            file_size = cache_file.stat().st_size
            logger.info(f"Bug data cached to {cache_file} ({file_size} bytes)")
            return cache_file
            
        except Exception as e:
            logger.error(f"Failed to save cache file: {e}")
            raise

    def generate_summary_report(self, bug_data: Dict[str, Any]) -> None:
        """Generate a human-readable summary report."""
        try:
            report_file = self.cache_dir / f"bug_summary_{self.bug_id}.md"
            
            with open(report_file, "w", encoding="utf-8") as f:
                f.write(f"# Bug Summary Report - {self.bug_id}\n\n")
                f.write(f"**Generated**: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}\\n\\n")
                
                # Basic info
                f.write("## Basic Information\n")
                f.write(f"- **Title**: {bug_data.get('title', 'N/A')}\\n")
                f.write(f"- **State**: {bug_data.get('state', 'N/A')}\\n")
                f.write(f"- **Priority**: {bug_data.get('priority', 'N/A')}\\n")
                f.write(f"- **Severity**: {bug_data.get('severity', 'N/A')}\\n")
                f.write(f"- **Assigned To**: {bug_data.get('assigned_to', 'Unassigned')}\\n")
                f.write(f"- **Created**: {bug_data.get('created_date', 'N/A')}\\n\\n")
                
                # Custom fields
                custom = bug_data.get('custom_fields', {})
                if any(custom.values()):
                    f.write("## Custom Fields\n")
                    for key, value in custom.items():
                        if value:
                            f.write(f"- **{key}**: {value}\\n")
                    f.write("\\n")
                
                # Description
                if bug_data.get('description'):
                    f.write("## Description\n")
                    f.write(f"{bug_data['description']}\\n\\n")
                
                # Repro steps
                if bug_data.get('repro_steps'):
                    f.write("## Reproduction Steps\n")
                    f.write(f"{bug_data['repro_steps']}\\n\\n")
                
                # Attachments
                if bug_data.get('attachments') or bug_data.get('screenshots'):
                    f.write("## Attachments\n")
                    for att in bug_data.get('attachments', []):
                        f.write(f"- {att.get('name', 'Unknown')}\\n")
                    for img in bug_data.get('screenshots', []):
                        f.write(f"- {img.get('name', 'Unknown')}\\n")
                    f.write("\\n")
                
                # Comments
                comments = bug_data.get('comments', [])
                if comments:
                    f.write(f"## Comments ({len(comments)})\n")
                    for i, comment in enumerate(comments[:5], 1):  # Show first 5
                        f.write(f"### Comment {i}\n")
                        f.write(f"{comment[:200]}{'...' if len(comment) > 200 else ''}\\n\\n")
                
                # Parent info
                parent = bug_data.get('parent', {})
                if parent and not parent.get('error'):
                    f.write("## Parent Work Item\n")
                    f.write(f"- **ID**: {parent.get('id', 'N/A')}\\n")
                    f.write(f"- **Type**: {parent.get('type', 'N/A')}\\n")
                    f.write(f"- **Title**: {parent.get('title', 'N/A')}\\n")
                    f.write(f"- **State**: {parent.get('state', 'N/A')}\\n\\n")
                
                # Processing stats
                stats = bug_data.get('processing_stats', {})
                f.write("## Processing Statistics\n")
                f.write(f"- **Processing Time**: {stats.get('processing_time_seconds', 0):.2f} seconds\\n")
                f.write(f"- **Attachments**: {stats.get('attachments_count', 0)}\\n")
                f.write(f"- **Screenshots**: {stats.get('screenshots_count', 0)}\\n")
                f.write(f"- **Comments**: {stats.get('comments_count', 0)}\\n")
                f.write(f"- **Has Parent**: {stats.get('has_parent', False)}\\n")
            
            logger.info(f"Summary report saved to {report_file}")
            
        except Exception as e:
            logger.warning(f"Failed to generate summary report: {e}")


def main():
    """Main execution function."""
    try:
        logger.info("Starting Azure DevOps bug fetcher")
        
        # Initialize fetcher
        fetcher = ADOBugFetcher()
        
        # Fetch bug details
        bug_data = fetcher.fetch_bug_details()
        
        # Validate critical data
        if not bug_data.get('title'):
            logger.warning("Bug has no title")
        if not bug_data.get('description') and not bug_data.get('repro_steps'):
            logger.warning("Bug has no description or repro steps")
        
        # Save to cache
        cache_file = fetcher.save_to_cache(bug_data)
        
        # Generate summary report
        fetcher.generate_summary_report(bug_data)
        
        # Success logging
        stats = bug_data.get('processing_stats', {})
        logger.info(f"Bug fetch completed successfully:")
        logger.info(f"   - Title: {bug_data.get('title', 'N/A')}")
        logger.info(f"   - State: {bug_data.get('state', 'N/A')}")
        logger.info(f"   - Priority: {bug_data.get('priority', 'N/A')}")
        logger.info(f"   - Attachments: {stats.get('attachments_count', 0)}")
        logger.info(f"   - Comments: {stats.get('comments_count', 0)}")
        logger.info(f"   - Processing time: {stats.get('processing_time_seconds', 0):.2f}s")
        
        return 0
        
    except Exception as e:
        logger.error(f"Bug fetcher failed: {e}")
        
        # Create minimal fallback data
        fallback_data = {
            "id": os.getenv("ADO_BUG_ID", "unknown"),
            "title": "Failed to fetch bug details",
            "description": f"Error: {str(e)}",
            "error": True,
            "error_message": str(e),
            "fetched_at": datetime.utcnow().isoformat() + "Z"
        }
        
        try:
            cache_dir = Path("cache")
            cache_dir.mkdir(exist_ok=True)
            cache_file = cache_dir / f"ado_bug_{fallback_data['id']}.json"
            
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(fallback_data, f, indent=2)
            
            logger.info(f"Fallback data saved to {cache_file}")
            
        except Exception as save_error:
            logger.error(f"Failed to save fallback data: {save_error}")
        
        return 1


if __name__ == "__main__":
    sys.exit(main())