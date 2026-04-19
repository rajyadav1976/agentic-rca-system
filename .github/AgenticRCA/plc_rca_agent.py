import os
import sys
import json
import time
import logging
import traceback
import signal
from datetime import datetime, timedelta
from typing import Any, List, Dict, Optional, Tuple
from pathlib import Path
from dataclasses import dataclass, asdict
from contextlib import contextmanager

try:
    import backoff
    import anthropic
except ImportError as e:
    print(f"Missing required package: {e}", file=sys.stderr)
    sys.exit(1)

# Import our enhanced tools (direct function calls)
try:
    from plc_rca_tools import (
        get_ado_bug_details, github_search_code, github_get_file_content,
        download_ado_attachment, report_rca_result, health_check
    )
except ImportError as e:
    print(f"Could not import RCA tools: {e}", file=sys.stderr)
    sys.exit(1)

# Configure comprehensive logging with directory creation
Path('logs').mkdir(exist_ok=True)
Path('output').mkdir(exist_ok=True)
Path('cache').mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('logs/rca_agent.log', mode='a')
    ]
)
logger = logging.getLogger(__name__)

@dataclass
class RCAConfig:
    """Configuration settings for RCA execution."""
    # Claude API settings
    max_claude_turns: int = 25
    claude_timeout: int = 300  # 5 minutes per API call
    claude_max_tokens: int = 4000
    claude_model: str = "claude-sonnet-4-20250514"  # Claude Sonnet 4.5
    
    # Execution limits
    max_tool_calls: int = 15
    max_execution_time: int = 600  # 10 minutes total
    force_completion_after_tools: int = 12
    force_completion_after_seconds: int = 240  # 4 minutes
    
    # Retry settings
    retry_delay: int = 2
    max_api_retries: int = 3
    
    # Content limits
    max_message_length: int = 50000
    preview_token_limit: int = 2000

@dataclass
class ExecutionMetrics:
    """Track execution metrics and statistics."""
    start_time: float
    turns_completed: int = 0
    tool_calls_made: int = 0
    api_calls_made: int = 0
    errors_encountered: int = 0
    cache_hits: int = 0
    total_tokens_used: int = 0
    final_status: str = "in_progress"
    completion_reason: str = ""

    def duration(self) -> float:
        return time.time() - self.start_time
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            **asdict(self),
            'duration_seconds': self.duration(),
            'avg_seconds_per_turn': self.duration() / max(1, self.turns_completed)
        }

class RCAAgent:
    """Production-ready RCA agent with comprehensive monitoring and error handling."""
    
    def __init__(self):
        # Environment validation
        self.bug_id = self._validate_env_var("ADO_BUG_ID")
        self.execution_id = os.getenv("RCA_EXECUTION_ID", f"rca_{int(time.time())}")

        # Initialize configuration
        self.config = RCAConfig()
        self.metrics = ExecutionMetrics(start_time=time.time())

        # Setup paths
        self.output_dir = Path("output")
        self.logs_dir = Path("logs")
        self.cache_dir = Path("cache")
        self.output_dir.mkdir(exist_ok=True)
        self.logs_dir.mkdir(exist_ok=True)
        self.cache_dir.mkdir(exist_ok=True)

        # Initialize Anthropic client for Claude API
        self.claude_api_key = self._validate_env_var("CLAUDE_API_KEY")
        self.claude_client = anthropic.Anthropic(api_key=self.claude_api_key)
        
        # GitHub token for repository access (used by tools)
        self.github_token = os.getenv("GITHUB_TOKEN") or os.getenv("GITHUB_PAT")
        if not self.github_token:
            logger.warning("No GITHUB_TOKEN or GITHUB_PAT set - GitHub API calls may be limited")

        # Message history
        self.messages = []

        # Control flags
        self.should_terminate = False
        self.force_completion = False

        # Setup signal handlers
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

        logger.info(f"RCA Agent initialized - Bug ID: {self.bug_id}, Execution ID: {self.execution_id}")
        logger.info(f"Using Claude model: {self.config.claude_model}")
    
    def _validate_env_var(self, var_name: str) -> str:
        """Validate required environment variable."""
        value = os.getenv(var_name)
        if not value:
            raise ValueError(f"Required environment variable {var_name} is not set")
        return value
    
    def _signal_handler(self, signum: int, frame) -> None:
        """Handle termination signals gracefully."""
        logger.warning(f"Received signal {signum} - initiating graceful shutdown")
        self.should_terminate = True
        self.metrics.completion_reason = f"signal_{signum}"
    
    def _load_base_prompt(self) -> str:
        """Load and customize the base RCA prompt."""
        prompt_path = Path("plc_rca_prompt.txt")
        if prompt_path.exists():
            with open(prompt_path, 'r', encoding='utf-8') as f:
                base_prompt = f.read()
        else:
            # Embedded fallback prompt
            base_prompt = """
You are an Expert Root Cause Analysis (RCA) Agent for software bugs.

Your task is to analyze a reported bug from Azure DevOps (ADO), investigate the GitHub repository, 
identify the root cause, and propose a deep-level fix using real code context.

CRITICAL INSTRUCTIONS:
- All RCA output MUST be in valid HTML format
- Use proper HTML tags: <h3>, <div>, <pre><code>
- HTML-escape all < and > characters in code blocks as &lt; and &gt;
- Always call report_rca_result when analysis is complete

Available tools:
- get_ado_bug_details: Get bug information
- github_search_code: Search repository code
- github_get_file_content: Read file contents
- download_ado_attachment: Process attachments
- report_rca_result: Submit final analysis
"""
        # Add execution context
        from datetime import timezone
        context_info = f"""

EXECUTION CONTEXT:
- Bug ID: {self.bug_id}
- Execution ID: {self.execution_id}
- Timestamp: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}
- Environment: GitHub Copilot MCP Server

You must ALWAYS finish by calling the `report_rca_result` tool with:
- summary: Brief overview of the issue
- root_cause: Technical details of the problem
- proposed_fix: Specific code changes needed
"""
        return base_prompt + context_info
    
    def _create_tool_definitions(self) -> List[Dict[str, Any]]:
        """Define tools available to Claude."""
        return [
            {
                "name": "get_ado_bug_details",
                "description": "Retrieve Azure DevOps bug details including description, steps, and attachments",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "bug_id": {"type": "integer", "description": "Azure DevOps bug ID"}
                    },
                    "required": ["bug_id"]
                }
            },
            {
                "name": "github_search_code",
                "description": "Search repository code with advanced relevance ranking",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query with keywords, filenames, or code patterns"}
                    },
                    "required": ["query"]
                }
            },
            {
                "name": "github_get_file_content",
                "description": "Read complete file content from repository",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File path relative to repository root"}
                    },
                    "required": ["path"]
                }
            },
            {
                "name": "download_ado_attachment",
                "description": "Download and process Azure DevOps attachment (logs, screenshots, etc.)",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "attachment_url": {"type": "string", "description": "URL of the attachment"},
                        "attachment_name": {"type": "string", "description": "Name of the attachment file"},
                        "content_type": {"type": "string", "description": "MIME type of the attachment"}
                    },
                    "required": ["attachment_url", "attachment_name", "content_type"]
                }
            },
            {
                "name": "report_rca_result",
                "description": "Submit final RCA analysis - MUST be called when analysis is complete",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "summary": {"type": "string", "description": "Brief summary of the issue and impact"},
                        "root_cause": {"type": "string", "description": "Technical root cause analysis in HTML format"},
                        "proposed_fix": {"type": "string", "description": "Specific proposed fix with code changes in HTML format"}
                    },
                    "required": ["summary", "root_cause", "proposed_fix"]
                }
            }
        ]
    
    def _initialize_conversation(self) -> None:
        """Initialize the conversation with Claude."""
        base_prompt = self._load_base_prompt()
        initial_message = f"""Azure DevOps Bug ID: {self.bug_id}

{base_prompt}

Begin by calling get_ado_bug_details to analyze the bug information, then proceed with your investigation using the available tools.
"""
        self.messages = [
            {
                "role": "user",
                "content": initial_message
            }
        ]
        logger.info(f"Conversation initialized with {len(initial_message)} characters")
    
    def _trim_message_context(self) -> List[Dict[str, Any]]:
        """Trim message context to stay within limits while preserving important information."""
        if not self.messages:
            return []
        
        # Calculate total length
        total_length = sum(len(str(msg.get("content", ""))) for msg in self.messages)
        
        if total_length <= self.config.max_message_length:
            return self.messages
        
        # Keep first message (system prompt) and recent messages
        trimmed = [self.messages[0]]  # Always keep system prompt
        
        # Add recent messages in reverse order until we hit the limit
        remaining_budget = self.config.max_message_length - len(str(self.messages[0]))
        
        for msg in reversed(self.messages[1:]):
            msg_length = len(str(msg.get("content", "")))
            if remaining_budget - msg_length > 0:
                trimmed.insert(-1, msg)  # Insert before the last (system) message
                remaining_budget -= msg_length
            else:
                break
        
        if len(trimmed) < len(self.messages):
            logger.info(f"Trimmed context: {len(self.messages)} -> {len(trimmed)} messages")
            # Add a marker message to indicate trimming
            trimmed.insert(1, {
                "role": "user",
                "content": f"[Previous {len(self.messages) - len(trimmed)} messages trimmed for context length]"
            })
        
        return trimmed
    
    def _log_interaction(self, request_data: Dict[str, Any], response_data: Any, 
                        operation: str = "claude_api") -> None:
        """Log interaction details for debugging."""
        try:
            # Create sanitized log entry
            log_entry = {
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "execution_id": self.execution_id,
                "operation": operation,
                "turn": self.metrics.turns_completed,
                "tool_calls": self.metrics.tool_calls_made
            }
            
            # Log to structured file
            log_file = self.logs_dir / f"interactions_{self.execution_id}.jsonl"
            with open(log_file, 'a', encoding='utf-8') as f:
                f.write(json.dumps(log_entry) + '\n')
                
        except Exception as e:
            logger.warning(f"Failed to log interaction: {e}")
    
    @backoff.on_exception(
        backoff.expo,
        (anthropic.APIError, anthropic.APIConnectionError, anthropic.RateLimitError),
        max_tries=3,
        max_time=60,
        on_backoff=lambda details: logger.warning(f"Retrying Claude API call - attempt {details['tries']}")
    )
    def _call_claude_api(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> Any:
        """Call Claude API directly using Anthropic SDK."""
        self.metrics.api_calls_made += 1
        
        try:
            logger.debug(f"Calling Claude API with {len(messages)} messages and {len(tools)} tools")
            
            response = self.claude_client.messages.create(
                model=self.config.claude_model,
                max_tokens=self.config.claude_max_tokens,
                messages=messages,
                tools=tools,
                tool_choice={"type": "auto"},
                temperature=0,
                timeout=self.config.claude_timeout
            )
            
            logger.debug(f"Claude API response: stop_reason={response.stop_reason}")
            
            # Convert response to dict format for consistent handling
            return {
                "content": [block.model_dump() for block in response.content],
                "stop_reason": response.stop_reason,
                "usage": response.usage.model_dump() if hasattr(response, 'usage') else {}
            }
            
        except anthropic.APIError as e:
            logger.error(f"Claude API error: {e}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error calling Claude API: {e}")
            raise
    
    def _execute_tool_call(self, tool_name: str, tool_input: Dict[str, Any]) -> Tuple[Any, bool]:
        """Execute a tool call and return result and success status."""
        try:
            logger.info(f"Executing tool: {tool_name} with input: {json.dumps(tool_input, default=str)[:200]}...")
            
            start_time = time.time()
            
            if tool_name == "get_ado_bug_details":
                result = get_ado_bug_details(tool_input["bug_id"])
            elif tool_name == "github_search_code":
                result = github_search_code(tool_input["query"])
            elif tool_name == "github_get_file_content":
                result = github_get_file_content(tool_input["path"])
            elif tool_name == "download_ado_attachment":
                result = download_ado_attachment(
                    tool_input["attachment_url"],
                    tool_input["attachment_name"],
                    tool_input["content_type"]
                )
            elif tool_name == "report_rca_result":
                result = report_rca_result(
                    tool_input["summary"],
                    tool_input["root_cause"],
                    tool_input["proposed_fix"]
                )
                # RCA completion - terminate successfully
                self.metrics.final_status = "completed"
                self.metrics.completion_reason = "rca_submitted"
                return result, True
            else:
                result = {"error": f"Unknown tool: {tool_name}"}
            
            duration = time.time() - start_time
            logger.info(f"Tool {tool_name} completed in {duration:.2f}s")
            
            # Log tool result size
            result_size = len(str(result))
            if result_size > 10000:
                logger.info(f"Large tool result: {result_size} characters")
            
            return result, False
            
        except Exception as e:
            logger.error(f"Tool execution failed: {tool_name} - {e}")
            traceback.print_exc()
            self.metrics.errors_encountered += 1
            return {"error": f"Tool execution failed: {str(e)}"}, False
    
    def _should_force_completion(self) -> bool:
        """Check if we should force RCA completion."""
        if self.force_completion:
            return True
        
        # Force completion conditions
        if self.metrics.tool_calls_made >= self.config.force_completion_after_tools:
            logger.info(f"Forcing completion - tool call limit reached ({self.metrics.tool_calls_made})")
            return True
        
        if self.metrics.duration() >= self.config.force_completion_after_seconds:
            logger.info(f"Forcing completion - time limit reached ({self.metrics.duration():.1f}s)")
            return True
        
        return False
    
    def _inject_completion_prompt(self) -> None:
        """Inject prompt to force RCA completion."""
        if self.force_completion:
            return  # Already injected
        
        completion_prompt = """
URGENT: You have gathered sufficient information for analysis. 

Do NOT make any more tool calls except report_rca_result. Based on the information you have collected, 
provide your final Root Cause Analysis immediately by calling report_rca_result with:

- summary: Brief overview of the issue
- root_cause: Technical analysis in proper HTML format  
- proposed_fix: Specific code changes in proper HTML format

Proceed with the RCA now using the data you have already gathered.
"""
        
        self.messages.append({
            "role": "user",
            "content": completion_prompt
        })
        
        self.force_completion = True
        logger.info("Completion prompt injected - forcing RCA finalization")
    
    def _process_claude_response(self, response: Any) -> bool:
        """Process Claude's response and execute any tool calls. Returns True if RCA completed."""
        if not response or "content" not in response:
            logger.warning("Empty response from Claude")
            return False
        
        rca_completed = False
        
        # Process each content block from Claude's response
        for block in response["content"]:
            if block.get("type") == "tool_use":
                self.metrics.tool_calls_made += 1
                tool_name = block.get("name")
                tool_input = block.get("input", {})
                
                # Add assistant message with tool use
                self.messages.append({
                    "role": "assistant",
                    "content": [block]
                })
                
                # Execute tool
                result, is_rca_complete = self._execute_tool_call(tool_name, tool_input)
                
                if is_rca_complete:
                    rca_completed = True
                
                # Add tool result as user message
                self.messages.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": block.get("id"),
                        "content": self._truncate_content(json.dumps(result, default=str))
                    }]
                })
                
            elif block.get("type") == "text":
                text_content = block.get("text", "")
                logger.info(f"Claude text: {text_content[:150].replace(chr(10), ' ')}...")
                self.messages.append({
                    "role": "assistant",
                    "content": text_content
                })
        
        return rca_completed
    
    def _truncate_content(self, content: str) -> str:
        """Truncate content to prevent context overflow."""
        if len(content) <= self.config.preview_token_limit:
            return content
        
        return content[:self.config.preview_token_limit] + f"\n\n[Content truncated - {len(content)} total characters]"
    
    def _create_fallback_rca(self) -> None:
        """Create fallback RCA when normal process fails."""
        logger.warning("Creating fallback RCA due to execution failure")
        
        fallback_summary = "RCA execution encountered errors and could not complete normally"
        fallback_root_cause = f"""
        <h3>EXECUTION ISSUE</h3>
        <div>
        The automated RCA process encountered technical difficulties:
        <ul>
        <li>Execution time: {self.metrics.duration():.1f} seconds</li>
        <li>Tool calls made: {self.metrics.tool_calls_made}</li>
        <li>API calls made: {self.metrics.api_calls_made}</li>
        <li>Errors encountered: {self.metrics.errors_encountered}</li>
        <li>Completion reason: {self.metrics.completion_reason}</li>
        </ul>
        </div>
        
        <h3>RECOMMENDATION</h3>
        <div>
        Manual investigation required by development team to analyze bug #{self.bug_id}.
        </div>
        """
        
        fallback_fix = """
        <div>
        <h3>MANUAL ANALYSIS REQUIRED</h3>
        <div>
        This bug requires manual investigation by the development team due to technical issues 
        with the automated analysis process.
        </div>
        </div>
        """
        
        try:
            report_rca_result(fallback_summary, fallback_root_cause, fallback_fix)
            self.metrics.final_status = "fallback_completed"
            logger.info("Fallback RCA generated successfully")
        except Exception as e:
            logger.error(f"Failed to generate fallback RCA: {e}")
            self.metrics.final_status = "failed"
    
    def _save_execution_report(self) -> None:
        """Save detailed execution report."""
        try:
            report_data = {
                "execution_id": self.execution_id,
                "bug_id": self.bug_id,
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "metrics": self.metrics.to_dict(),
                "config": asdict(self.config),
                "message_count": len(self.messages),
                "final_status": self.metrics.final_status
            }
            
            report_file = self.logs_dir / f"execution_report_{self.execution_id}.json"
            with open(report_file, 'w', encoding='utf-8') as f:
                json.dump(report_data, f, indent=2, default=str)
            
            logger.info(f"Execution report saved: {report_file}")
            
        except Exception as e:
            logger.error(f"Failed to save execution report: {e}")
    
    @contextmanager
    def _execution_timeout(self):
        """Context manager for execution timeout (cross-platform)."""
        import threading
        timer = None
        def timeout_handler():
            logger.error("Execution timeout reached")
            self.should_terminate = True
            self.metrics.completion_reason = "timeout"
        try:
            timer = threading.Timer(self.config.max_execution_time, timeout_handler)
            timer.start()
            yield
        finally:
            if timer:
                timer.cancel()
    
    def run(self) -> int:
        """Main execution method."""
        logger.info(f"Starting RCA execution for bug {self.bug_id}")
        
        try:
            # Perform health check
            health_status = health_check()
            if health_status.get("overall_status") != "healthy":
                logger.warning(f"Health check issues: {health_status}")
            
            # Initialize conversation
            self._initialize_conversation()
            
            # Define tools
            tools = self._create_tool_definitions()
            
            with self._execution_timeout():
                # Main interaction loop
                for turn in range(1, self.config.max_claude_turns + 1):
                    if self.should_terminate:
                        logger.info("Termination requested - exiting loop")
                        break
                    
                    self.metrics.turns_completed = turn
                    logger.info(f"Turn {turn}/{self.config.max_claude_turns}")
                    
                    # Check completion conditions
                    if self._should_force_completion():
                        self._inject_completion_prompt()
                    
                    # Prepare messages
                    trimmed_messages = self._trim_message_context()
                    
                    # Add delay to respect rate limits
                    if turn > 1:
                        time.sleep(self.config.retry_delay)
                    
                    try:
                        # Call Claude API
                        response = self._call_claude_api(trimmed_messages, tools)
                        
                        # Process response
                        rca_completed = self._process_claude_response(response)
                        
                        if rca_completed:
                            logger.info("RCA completed successfully")
                            self.metrics.final_status = "completed"
                            break
                        
                        # Check if Claude stopped without tool use
                        if response["stop_reason"] == "stop" and not any(
                            (isinstance(block, dict) and block.get("type") == "tool_use") for block in response["content"]
                        ):
                            logger.warning("Claude stopped without tool use - forcing completion")
                            self._inject_completion_prompt()
                    
                    except Exception as e:
                        logger.error(f"Error in turn {turn}: {e}")
                        self.metrics.errors_encountered += 1
                        
                        if self.metrics.errors_encountered >= 3:
                            logger.error("Too many errors - terminating")
                            self.metrics.completion_reason = "too_many_errors"
                            break
                
                # Handle completion
                if self.metrics.final_status != "completed":
                    if not self.should_terminate:
                        logger.warning(f"RCA not completed normally - creating fallback (status: {self.metrics.final_status})")
                        self._create_fallback_rca()
                    else:
                        self.metrics.final_status = "terminated"
            
            # Save execution report
            self._save_execution_report()
            
            # Log final statistics
            duration = self.metrics.duration()
            logger.info(f"Execution completed:")
            logger.info(f"   - Status: {self.metrics.final_status}")
            logger.info(f"   - Duration: {duration:.2f}s")
            logger.info(f"   - Turns: {self.metrics.turns_completed}")
            logger.info(f"   - Tool calls: {self.metrics.tool_calls_made}")
            logger.info(f"   - API calls: {self.metrics.api_calls_made}")
            logger.info(f"   - Errors: {self.metrics.errors_encountered}")
            
            # Return appropriate exit code
            return 0 if self.metrics.final_status in ["completed", "fallback_completed"] else 1
            
        except Exception as e:
            logger.error(f"Fatal error in RCA execution: {e}")
            traceback.print_exc()
            
            self.metrics.final_status = "fatal_error"
            self.metrics.completion_reason = str(e)
            
            # Attempt to save error report
            try:
                self._save_execution_report()
            except Exception as report_error:
                logger.error(f"Failed to save error report: {report_error}")
            
            return 1


def main() -> int:
    """Main entry point."""
    try:
        # Create and run RCA agent
        agent = RCAAgent()
        return agent.run()
        
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        return 130
    except Exception as e:
        logger.error(f"Fatal initialization error: {e}")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())