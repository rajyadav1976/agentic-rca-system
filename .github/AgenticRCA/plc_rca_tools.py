import sys
import os
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

import json
import re
import base64
import time
import logging
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple, Union
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

try:
    import requests
    import backoff
except ImportError as e:
    print(f"Missing required package: {e}", file=sys.stderr)
    sys.exit(1)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

@dataclass
class SearchResult:
    """Structured search result with metadata."""
    path: str
    url: str
    snippet: str
    score: float
    file_type: str
    size_bytes: int
    last_modified: str

@dataclass
class FileContent:
    """Structured file content with metadata."""
    path: str
    content: str
    size_bytes: int
    lines: int
    encoding: str
    last_modified: str

class RCAToolsConfig:
    """Configuration management for RCA tools."""
    
    def __init__(self):
        self.repo_path = os.getenv("REPO_PATH", "/tmp/rca/code")
        self.cache_dir = Path("cache")
        self.cache_dir.mkdir(exist_ok=True)
        
        # GitHub configuration
        self.github_token = os.getenv("GITHUB_TOKEN")
        self.github_repo = os.getenv("GITHUB_REPOSITORY", "")
        
        # ADO configuration
        self.ado_pat = os.getenv("ADO_PAT", "")
        self.ado_org_url = os.getenv("ADO_ORG_URL", "")
        self.ado_project = os.getenv("ADO_PROJECT_NAME", "")
        self.ado_bug_id = os.getenv("ADO_BUG_ID", "")
        
        # Search configuration
        self.max_search_results = 15
        self.max_file_size = 1024 * 1024  # 1MB
        self.search_timeout = 30
        self.cache_ttl = 3600  # 1 hour
        
        # File type priorities for search ranking
        self.priority_extensions = {'.cs': 1, '.aspx': 2, '.ts': 3, '.tsx': 4, '.js': 5}
        self.ignore_patterns = {'.git', 'node_modules', 'bin', 'obj', '.vs', 'packages'}
        
        self._validate_config()
    
    def _validate_config(self):
        """Validate configuration and log warnings for missing values."""
        if not Path(self.repo_path).exists():
            logger.warning(f"Repository path does not exist: {self.repo_path}")
        
        if not self.github_token:
            logger.warning("GITHUB_TOKEN not set - GitHub API calls may fail")
        
        if not self.ado_pat:
            logger.warning("ADO_PAT not set - ADO operations may fail")

class CacheManager:
    """Intelligent caching system for search results and file contents."""
    
    def __init__(self, cache_dir: Path, ttl: int = 3600):
        self.cache_dir = cache_dir
        self.ttl = ttl
        self.cache_dir.mkdir(exist_ok=True)
    
    def _get_cache_key(self, operation: str, params: Dict[str, Any]) -> str:
        """Generate cache key from operation and parameters."""
        params_str = json.dumps(params, sort_keys=True)
        return hashlib.md5(f"{operation}:{params_str}".encode()).hexdigest()
    
    def get(self, operation: str, params: Dict[str, Any]) -> Optional[Any]:
        """Retrieve cached result if valid."""
        cache_key = self._get_cache_key(operation, params)
        cache_file = self.cache_dir / f"{cache_key}.json"
        
        try:
            if cache_file.exists():
                stat = cache_file.stat()
                if time.time() - stat.st_mtime < self.ttl:
                    with open(cache_file, 'r', encoding='utf-8') as f:
                        cached_data = json.load(f)
                    logger.debug(f"Cache hit for {operation}")
                    return cached_data['result']
                else:
                    # Remove expired cache
                    cache_file.unlink()
                    logger.debug(f"Expired cache removed for {operation}")
        except Exception as e:
            logger.warning(f"Cache read error: {e}")
        
        return None
    
    def set(self, operation: str, params: Dict[str, Any], result: Any) -> None:
        """Store result in cache."""
        cache_key = self._get_cache_key(operation, params)
        cache_file = self.cache_dir / f"{cache_key}.json"
        
        try:
            cache_data = {
                'timestamp': time.time(),
                'operation': operation,
                'params': params,
                'result': result
            }
            with open(cache_file, 'w', encoding='utf-8') as f:
                json.dump(cache_data, f, indent=2, default=str)
            logger.debug(f"Cached result for {operation}")
        except Exception as e:
            logger.warning(f"Cache write error: {e}")

class GitHubCodeSearcher:
    """Advanced GitHub code search with local fallback."""
    
    def __init__(self, config: RCAToolsConfig):
        self.config = config
        self.cache = CacheManager(config.cache_dir / "search", config.cache_ttl)
        
        # Compile regex patterns
        self.non_word_re = re.compile(r'\W+')
        self.whitespace_re = re.compile(r'\s+')
        
        # GitHub API session
        self.session = requests.Session()
        if config.github_token:
            self.session.headers.update({
                'Authorization': f'token {config.github_token}',
                'Accept': 'application/vnd.github.v3+json'
            })
    
    def _tokenize_query(self, query: str) -> List[str]:
        """Extract meaningful tokens from search query."""
        tokens = [t.lower() for t in self.non_word_re.split(query) if t and len(t) > 1]
        # Remove common stop words
        stop_words = {'the', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'of', 'with', 'by'}
        return [t for t in tokens if t not in stop_words]
    
    def _build_phrase_regex(self, tokens: List[str]) -> re.Pattern:
        """Build regex for phrase matching."""
        if not tokens:
            return re.compile(r'$^')  # Never matches
        escaped_tokens = [re.escape(token) for token in tokens]
        pattern = r'\W+'.join(escaped_tokens)
        return re.compile(pattern, re.IGNORECASE | re.DOTALL)
    
    def _should_ignore_path(self, path: str) -> bool:
        """Check if path should be ignored."""
        path_lower = path.lower()
        return any(pattern in path_lower for pattern in self.config.ignore_patterns)
    
    def _calculate_relevance_score(self, file_path: str, content: str, tokens: List[str], 
                                 phrase_match: bool) -> float:
        """Calculate relevance score for search result."""
        score = 0.0
        path_lower = file_path.lower()
        content_lower = content.lower()
        
        # File extension priority
        ext = Path(file_path).suffix.lower()
        if ext in self.config.priority_extensions:
            score += (10 - self.config.priority_extensions[ext]) * 2
        
        # Token matches in filename
        filename_tokens = sum(1 for token in tokens if token in path_lower)
        score += filename_tokens * 5
        
        # Token matches in content
        content_tokens = sum(1 for token in tokens if token in content_lower)
        score += content_tokens * 2
        
        # Phrase match bonus
        if phrase_match:
            score += 10
        
        # Penalize very large files
        if len(content) > 50000:
            score -= 2
        
        # Boost for likely source files
        if any(keyword in path_lower for keyword in ['controller', 'service', 'model', 'component']):
            score += 3
        
        return score
    
    def _search_local_files(self, tokens: List[str], phrase_regex: re.Pattern) -> List[SearchResult]:
        """Search local repository files."""
        results = []
        repo_path = Path(self.config.repo_path)
        
        if not repo_path.exists():
            logger.warning(f"Repository path not found: {repo_path}")
            return results
        
        def process_file(file_path: Path) -> Optional[SearchResult]:
            try:
                if self._should_ignore_path(str(file_path)):
                    return None
                
                # Skip large files
                if file_path.stat().st_size > self.config.max_file_size:
                    return None
                
                # Read file content
                try:
                    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                        content = f.read(10000)  # Read first 10KB for search
                except (UnicodeDecodeError, PermissionError):
                    return None
                
                # Check for token matches
                content_lower = content.lower()
                path_lower = str(file_path).lower()
                
                # Quick token check
                if not any(token in content_lower or token in path_lower for token in tokens):
                    return None
                
                # Find phrase match
                phrase_match = phrase_regex.search(content_lower)
                
                # Calculate score
                rel_path = file_path.relative_to(repo_path)
                score = self._calculate_relevance_score(str(rel_path), content, tokens, bool(phrase_match))
                
                if score < 1:  # Minimum threshold
                    return None
                
                # Extract snippet
                if phrase_match:
                    start = max(0, phrase_match.start() - 100)
                    end = min(len(content), phrase_match.end() + 100)
                    snippet = content[start:end]
                else:
                    # Find first token occurrence
                    for token in tokens:
                        idx = content_lower.find(token)
                        if idx >= 0:
                            start = max(0, idx - 100)
                            end = min(len(content), idx + 200)
                            snippet = content[start:end]
                            break
                    else:
                        snippet = content[:200]
                
                # Clean snippet
                snippet = self.whitespace_re.sub(' ', snippet).strip()
                if len(snippet) > 300:
                    snippet = snippet[:297] + "..."
                
                return SearchResult(
                    path=str(rel_path),
                    url=f"file://{file_path}",
                    snippet=snippet,
                    score=score,
                    file_type=file_path.suffix.lower(),
                    size_bytes=file_path.stat().st_size,
                    last_modified=datetime.fromtimestamp(file_path.stat().st_mtime).isoformat()
                )
                
            except Exception as e:
                logger.debug(f"Error processing {file_path}: {e}")
                return None
        
        # Collect all files
        try:
            all_files = []
            for root, dirs, files in os.walk(repo_path):
                # Skip ignored directories
                dirs[:] = [d for d in dirs if not any(pattern in d.lower() for pattern in self.config.ignore_patterns)]
                
                for file in files:
                    file_path = Path(root) / file
                    if file_path.is_file():
                        all_files.append(file_path)
            
            logger.info(f"Searching {len(all_files)} files")
            
            # Process files in parallel
            with ThreadPoolExecutor(max_workers=4) as executor:
                future_to_file = {executor.submit(process_file, fp): fp for fp in all_files}
                
                for future in as_completed(future_to_file, timeout=self.config.search_timeout):
                    try:
                        result = future.result()
                        if result:
                            results.append(result)
                    except Exception as e:
                        logger.debug(f"Future error: {e}")
            
        except Exception as e:
            logger.error(f"Error during file search: {e}")
        
        # Sort by relevance score
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:self.config.max_search_results]
    
    @backoff.on_exception(backoff.expo, requests.RequestException, max_tries=3)
    def _search_github_api(self, query: str) -> List[SearchResult]:
        """Search using GitHub Code Search API."""
        if not self.config.github_token or not self.config.github_repo:
            return []
        
        try:
            search_url = "https://api.github.com/search/code"
            params = {
                'q': f'{query} repo:{self.config.github_repo}',
                'per_page': min(self.config.max_search_results, 30)
            }
            
            response = self.session.get(search_url, params=params, timeout=10)
            response.raise_for_status()
            
            data = response.json()
            results = []
            
            for item in data.get('items', []):
                results.append(SearchResult(
                    path=item['path'],
                    url=item['html_url'],
                    snippet=item.get('text_matches', [{}])[0].get('fragment', '')[:300],
                    score=item.get('score', 0),
                    file_type=Path(item['path']).suffix.lower(),
                    size_bytes=0,  # Not provided by API
                    last_modified=""  # Not provided by API
                ))
            
            logger.info(f"GitHub API returned {len(results)} results")
            return results
            
        except requests.RequestException as e:
            logger.warning(f"GitHub API search failed: {e}")
            return []
        except Exception as e:
            logger.error(f"Unexpected error in GitHub API search: {e}")
            return []
    
    def search_code(self, query: str) -> str:
        """Main search method that combines local and API results."""
        if not query.strip():
            return json.dumps([])
        
        start_time = time.time()
        cache_params = {'query': query, 'max_results': self.config.max_search_results}
        
        # Check cache first
        cached_result = self.cache.get('search_code', cache_params)
        if cached_result is not None:
            logger.info(f"Using cached search results for: {query}")
            return json.dumps(cached_result)
        
        logger.info(f"Searching for: {query}")
        
        # Tokenize query
        tokens = self._tokenize_query(query)
        if not tokens:
            return json.dumps([])
        
        phrase_regex = self._build_phrase_regex(tokens)
        
        # Search local files
        local_results = self._search_local_files(tokens, phrase_regex)
        
        # Search GitHub API (if available)
        api_results = self._search_github_api(query)
        
        # Combine and deduplicate results
        all_results = {}
        for result in local_results + api_results:
            key = result.path.lower()
            if key not in all_results or result.score > all_results[key].score:
                all_results[key] = result
        
        # Convert to JSON-serializable format
        final_results = []
        for result in sorted(all_results.values(), key=lambda r: r.score, reverse=True):
            final_results.append({
                'path': result.path,
                'url': result.url,
                'snippet': result.snippet,
                'score': result.score,
                'file_type': result.file_type,
                'size_bytes': result.size_bytes,
                'last_modified': result.last_modified
            })
        
        # Limit results
        final_results = final_results[:self.config.max_search_results]
        
        # Cache results
        self.cache.set('search_code', cache_params, final_results)
        
        duration = time.time() - start_time
        logger.info(f"Search completed: {len(final_results)} results in {duration:.2f}s")
        
        return json.dumps(final_results, indent=2)

class FileContentManager:
    """Enhanced file content retrieval with caching and validation."""
    
    def __init__(self, config: RCAToolsConfig):
        self.config = config
        self.cache = CacheManager(config.cache_dir / "files", config.cache_ttl)
        
        # GitHub API session
        self.session = requests.Session()
        if config.github_token:
            self.session.headers.update({
                'Authorization': f'token {config.github_token}',
                'Accept': 'application/vnd.github.v3.raw'
            })
    
    def _read_local_file(self, file_path: Path) -> Optional[FileContent]:
        """Read file from local filesystem."""
        try:
            if not file_path.exists():
                return None
            
            stat = file_path.stat()
            if stat.st_size > self.config.max_file_size:
                logger.warning(f"File too large: {file_path} ({stat.st_size} bytes)")
                return None
            
            # Detect encoding
            encoding = 'utf-8'
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read()
            except UnicodeDecodeError:
                # Try common encodings
                for enc in ['latin-1', 'cp1252', 'iso-8859-1']:
                    try:
                        with open(file_path, 'r', encoding=enc) as f:
                            content = f.read()
                        encoding = enc
                        break
                    except UnicodeDecodeError:
                        continue
                else:
                    logger.error(f"Could not decode file: {file_path}")
                    return None
            
            return FileContent(
                path=str(file_path),
                content=content,
                size_bytes=stat.st_size,
                lines=content.count('\n') + 1,
                encoding=encoding,
                last_modified=datetime.fromtimestamp(stat.st_mtime).isoformat()
            )
            
        except Exception as e:
            logger.error(f"Error reading local file {file_path}: {e}")
            return None
    
    @backoff.on_exception(backoff.expo, requests.RequestException, max_tries=3)
    def _read_github_file(self, path: str) -> Optional[FileContent]:
        """Read file from GitHub API."""
        if not self.config.github_token or not self.config.github_repo:
            return None
        
        try:
            url = f"https://api.github.com/repos/{self.config.github_repo}/contents/{path}"
            response = self.session.get(url, timeout=10)
            
            if response.status_code == 404:
                return None
            
            response.raise_for_status()
            content = response.text
            
            return FileContent(
                path=path,
                content=content,
                size_bytes=len(content.encode('utf-8')),
                lines=content.count('\n') + 1,
                encoding='utf-8',
                last_modified=datetime.utcnow().isoformat()
            )
            
        except requests.RequestException as e:
            logger.warning(f"GitHub API file read failed for {path}: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error reading GitHub file {path}: {e}")
            return None
    
    def _find_file_candidates(self, path: str) -> List[Path]:
        """Find potential file matches using various strategies."""
        repo_path = Path(self.config.repo_path)
        candidates = []
        
        # Direct path match
        direct_path = repo_path / path
        if direct_path.is_file():
            candidates.append(direct_path)
        
        # Common prefix additions
        common_prefixes = [
            "ProfileTailorApp",
            "src", "source", "app", "web",
            "Views", "Controllers", "Models", "Services"
        ]
        
        for prefix in common_prefixes:
            prefixed_path = repo_path / prefix / path
            if prefixed_path.is_file():
                candidates.append(prefixed_path)
        
        # Filename-only search
        filename = os.path.basename(path)
        if filename != path:
            try:
                for found_path in repo_path.rglob(filename):
                    if found_path.is_file() and found_path not in candidates:
                        candidates.append(found_path)
                        if len(candidates) >= 5:  # Limit candidates
                            break
            except Exception as e:
                logger.debug(f"Error in filename search: {e}")
        
        return candidates
    
    def get_file_content(self, path: str) -> Union[str, Dict[str, str]]:
        """Main method to retrieve file content with multiple fallback strategies."""
        if not path.strip():
            return {"error": "Empty file path provided"}
        
        start_time = time.time()
        original_path = path
        
        # Check cache first
        cache_params = {'path': path}
        cached_result = self.cache.get('file_content', cache_params)
        if cached_result is not None:
            logger.info(f"Using cached file content for: {path}")
            return cached_result
        
        logger.info(f"Reading file: {path}")
        
        # Strategy 1: Local file system
        candidates = self._find_file_candidates(path)
        
        for candidate in candidates:
            file_content = self._read_local_file(candidate)
            if file_content:
                result = file_content.content
                self.cache.set('file_content', cache_params, result)
                
                duration = time.time() - start_time
                logger.info(f"File read successfully from local: {candidate} ({file_content.size_bytes} bytes, {duration:.2f}s)")
                return result
        
        # Strategy 2: GitHub API
        if self.config.github_token:
            file_content = self._read_github_file(path)
            if file_content:
                result = file_content.content
                self.cache.set('file_content', cache_params, result)
                
                duration = time.time() - start_time
                logger.info(f"File read successfully from GitHub API: {path} ({file_content.size_bytes} bytes, {duration:.2f}s)")
                return result
        
        # Strategy 3: Content-based search
        logger.info(f"File not found directly, searching by content...")
        try:
            searcher = GitHubCodeSearcher(self.config)
            search_results = json.loads(searcher.search_code(os.path.basename(path)))
            
            for result in search_results[:3]:  # Try top 3 matches
                result_path = result.get('path', '')
                if result_path:
                    file_content = self._read_local_file(Path(self.config.repo_path) / result_path)
                    if file_content:
                        result_content = file_content.content
                        self.cache.set('file_content', cache_params, result_content)
                        
                        duration = time.time() - start_time
                        logger.info(f"File found via search: {result_path} ({file_content.size_bytes} bytes, {duration:.2f}s)")
                        return result_content
        except Exception as e:
            logger.warning(f"Content-based search failed: {e}")
        
        # All strategies failed
        error_msg = {
            "error": f"File not found: {original_path}",
            "searched_candidates": [str(c) for c in candidates],
            "repo_path": self.config.repo_path,
            "search_attempted": True
        }
        
        duration = time.time() - start_time
        logger.warning(f"File not found after {duration:.2f}s: {original_path}")
        
        return error_msg

class ADOIntegration:
    """Azure DevOps integration for attachments and bug details."""
    
    def __init__(self, config: RCAToolsConfig):
        self.config = config
        self.cache = CacheManager(config.cache_dir / "ado", config.cache_ttl)
        
        # ADO API session
        self.session = requests.Session()
        if config.ado_pat:
            auth_header = base64.b64encode(f":{config.ado_pat}".encode()).decode()
            self.session.headers.update({
                'Authorization': f'Basic {auth_header}',
                'Content-Type': 'application/json'
            })
    
    def _ocr_image(self, image_bytes: bytes) -> str:
        """Attempt OCR on image bytes with fallback."""
        try:
            from PIL import Image
            import pytesseract
            import io
            
            img = Image.open(io.BytesIO(image_bytes))
            text = pytesseract.image_to_string(img)
            return f"OCR_CONTENT_START\n{text.strip()}\nOCR_CONTENT_END"
        except ImportError:
            return "[OCR_UNAVAILABLE - pytesseract not installed]"
        except Exception as e:
            return f"[OCR_FAILED - {str(e)}]"
    
    @backoff.on_exception(backoff.expo, requests.RequestException, max_tries=3)
    def download_attachment(self, attachment_url: str, attachment_name: str, 
                          content_type: str) -> Dict[str, Any]:
        """Download and process ADO attachment with comprehensive error handling."""
        if not self.config.ado_pat:
            return {"error": "ADO_PAT not configured for attachment download"}
        
        start_time = time.time()
        cache_params = {'attachment_url': attachment_url}
        
        # Check cache first
        cached_result = self.cache.get('download_attachment', cache_params)
        if cached_result is not None:
            logger.info(f"Using cached attachment: {attachment_name}")
            return cached_result
        
        logger.info(f"Downloading attachment: {attachment_name}")
        
        try:
            response = self.session.get(attachment_url, stream=True, timeout=30)
            response.raise_for_status()
            
            actual_content_type = response.headers.get("Content-Type", content_type)
            content_length = int(response.headers.get("Content-Length", 0))
            
            # Size check
            if content_length > 10 * 1024 * 1024:  # 10MB limit
                return {
                    "error": f"Attachment too large: {content_length} bytes",
                    "name": attachment_name,
                    "url": attachment_url
                }
            
            # Read content
            content_bytes = response.content
            
            # Process based on content type
            if any(t in actual_content_type.lower() for t in ["text", "json", "xml", "csv"]):
                try:
                    content = content_bytes.decode('utf-8', errors='replace')
                except Exception:
                    content = content_bytes.decode('latin-1', errors='replace')
                    
            elif "image" in actual_content_type.lower():
                content = self._ocr_image(content_bytes)
                
            else:
                # Binary content - encode as base64
                content = base64.b64encode(content_bytes).decode('ascii')
                content = f"[BINARY_CONTENT - Base64 encoded, {len(content_bytes)} bytes]\n{content[:1000]}..."
            
            result = {
                "name": attachment_name,
                "url": attachment_url,
                "content_type": actual_content_type,
                "size_bytes": len(content_bytes),
                "content": content,
                "downloaded_at": datetime.utcnow().isoformat() + "Z"
            }
            
            # Cache result
            self.cache.set('download_attachment', cache_params, result)
            
            duration = time.time() - start_time
            logger.info(f"Attachment downloaded: {attachment_name} ({len(content_bytes)} bytes, {duration:.2f}s)")
            
            return result
            
        except requests.exceptions.RequestException as e:
            error_result = {
                "error": f"Download failed: {str(e)}",
                "name": attachment_name,
                "url": attachment_url,
                "content_type": content_type
            }
            logger.error(f"Failed to download attachment {attachment_name}: {e}")
            return error_result
            
        except Exception as e:
            error_result = {
                "error": f"Processing failed: {str(e)}",
                "name": attachment_name,
                "url": attachment_url,
                "content_type": content_type
            }
            logger.error(f"Failed to process attachment {attachment_name}: {e}")
            return error_result
    
    def get_bug_details(self, bug_id: int) -> str:
        """Retrieve bug details from cache or ADO API."""
        try:
            # First try cache file
            cache_file = self.config.cache_dir / f"ado_bug_{bug_id}.json"
            if cache_file.exists():
                with open(cache_file, 'r', encoding='utf-8') as f:
                    cached_data = f.read()
                logger.info(f"Using cached bug details for {bug_id}")
                return cached_data
            
            # If no cache, return minimal structure
            logger.warning(f"No cached bug details found for {bug_id}")
            fallback_data = {
                "id": bug_id,
                "title": f"Bug {bug_id} - Details not available",
                "description": "Bug details were not fetched successfully",
                "error": "No cached data available",
                "fetched_at": datetime.utcnow().isoformat() + "Z"
            }
            return json.dumps(fallback_data, indent=2)
            
        except Exception as e:
            logger.error(f"Failed to get bug details for {bug_id}: {e}")
            error_data = {
                "id": bug_id,
                "error": str(e),
                "fetched_at": datetime.utcnow().isoformat() + "Z"
            }
            return json.dumps(error_data, indent=2)

class RCAReporter:
    """Generate and format RCA results."""
    
    def __init__(self, config: RCAToolsConfig):
        self.config = config
        self.output_dir = Path("output")
        self.output_dir.mkdir(exist_ok=True)
    
    def report_rca_result(self, summary: str, root_cause: str, proposed_fix: str) -> str:
        """Generate final RCA report in HTML format."""
        from datetime import timezone
        timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')

        # Validate inputs
        if not summary.strip():
            summary = "RCA Summary not provided"
        if not root_cause.strip():
            root_cause = "Root cause analysis not completed"
        if not proposed_fix.strip():
            proposed_fix = "No fix proposed"

        # Generate HTML report
        html_content = f"""
        <div style="font-family: Arial, sans-serif; max-width: 1200px; margin: 0 auto; padding: 20px;">
            <div style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 20px; border-radius: 8px; margin-bottom: 20px;">
                <h1 style="margin: 0; display: flex; align-items: center;">
                    AI-Generated Root Cause Analysis                    
                </h1>
            </div>
            
            <div style="background: #f8f9fa; border-left: 4px solid #28a745; padding: 20px; margin-bottom: 20px; border-radius: 4px;">
                <h2 style="color: #28a745; margin-top: 0;">Summary</h2>
                <div style="line-height: 1.6;">
                    {summary}
                </div>
            </div>
            
            <div style="background: #fff3cd; border-left: 4px solid #ffc107; padding: 20px; margin-bottom: 20px; border-radius: 4px;">
                <h2 style="color: #856404; margin-top: 0;">Root Cause</h2>
                <div style="line-height: 1.6;">
                    {root_cause}
                </div>
            </div>
            
            <div style="background: #d1ecf1; border-left: 4px solid #17a2b8; padding: 20px; margin-bottom: 20px; border-radius: 4px;">
                <h2 style="color: #0c5460; margin-top: 0;">Proposed Fix</h2>
                <div style="line-height: 1.6;">
                    {proposed_fix}
                </div>
            </div>
        </div>
        """

        # Save HTML report
        html_file = self.output_dir / "rca_output.html"
        try:
            with open(html_file, 'w', encoding='utf-8') as f:
                f.write(html_content)
            logger.info(f"RCA HTML report saved: {html_file}")
        except Exception as e:
            logger.error(f"Failed to save HTML report: {e}")
        
        # Save JSON report
        json_data = {
            "bug_id": self.config.ado_bug_id,
            "generated_at": timestamp,
            "execution_id": os.getenv('RCA_EXECUTION_ID', 'unknown'),
            "summary": summary,
            "root_cause": root_cause,
            "proposed_fix": proposed_fix
        }
        
        json_file = self.output_dir / "rca_output.json"
        try:
            with open(json_file, 'w', encoding='utf-8') as f:
                json.dump(json_data, f, indent=2, ensure_ascii=False)
            logger.info(f"RCA JSON report saved: {json_file}")
        except Exception as e:
            logger.error(f"Failed to save JSON report: {e}")
        
        # Output delimiters for workflow parsing
        print("::claude_rca_output_start::")
        print(html_content)
        print("::claude_rca_output_end::")
        
        return "RCA result reported successfully"

# Global instances for tool functions
_config = RCAToolsConfig()
_searcher = GitHubCodeSearcher(_config)
_file_manager = FileContentManager(_config)
_ado_integration = ADOIntegration(_config)
_reporter = RCAReporter(_config)

# Tool function implementations for MCP integration
def github_search_code(query: str) -> str:
    """Search GitHub repository code with advanced relevance ranking."""
    return _searcher.search_code(query)

def github_get_file_content(path: str) -> Union[str, Dict[str, str]]:
    """Get file content with multiple fallback strategies."""
    return _file_manager.get_file_content(path)

def get_ado_bug_details(bug_id: int) -> str:
    """Get Azure DevOps bug details from cache."""
    return _ado_integration.get_bug_details(bug_id)

def download_ado_attachment(attachment_url: str, attachment_name: str, 
                           content_type: str) -> Dict[str, Any]:
    """Download and process Azure DevOps attachment."""
    return _ado_integration.download_attachment(attachment_url, attachment_name, content_type)

def report_rca_result(summary: str, root_cause: str, proposed_fix: str) -> str:
    """Generate final RCA report."""
    return _reporter.report_rca_result(summary, root_cause, proposed_fix)

# Health check function for monitoring tool components
def health_check() -> Dict[str, Any]:
    """Perform health check of all tool components."""
    checks = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "repo_path_exists": Path(_config.repo_path).exists(),
        "cache_dir_writable": _config.cache_dir.exists() and os.access(_config.cache_dir, os.W_OK),
        "github_token_configured": bool(_config.github_token),
        "ado_pat_configured": bool(_config.ado_pat),
        "bug_id_set": bool(_config.ado_bug_id)
    }
    checks["overall_status"] = "healthy" if all(checks.values()) else "degraded"
    logger.info(f"Health check: {checks['overall_status']}")
    return checks

if __name__ == "__main__":
    # Self-test mode
    print("Running RCA Tools self-test...")
    health = health_check()
    print(json.dumps(health, indent=2))
    
    if len(sys.argv) > 1:
        test_query = " ".join(sys.argv[1:])
        print(f"\nTest search: {test_query}")
        results = github_search_code(test_query)
        print(results)