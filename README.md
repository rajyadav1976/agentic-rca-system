# Agentic RCA System  
### Autonomous AI Agent for Root Cause Analysis of Enterprise Software Bugs

## Overview
Built an autonomous, agentic AI system that performs end-to-end root cause analysis (RCA) for enterprise software bugs — from ticket ingestion to generating developer-ready reports — reducing investigation time from hours to minutes.

Unlike traditional LLM wrappers, this system uses a **multi-step reasoning agent with dynamic tool selection**, grounded in real code and production data.

---

## Problem Statement
Root Cause Analysis (RCA) in large-scale enterprise systems is:

- Time-intensive (2–4 hours per bug)
- Highly manual (reading tickets, tracing code, analyzing logs)
- Inconsistent (depends on engineer experience)

At scale, this leads to:
- Slower delivery cycles  
- High context-switching overhead  
- Inefficient use of senior engineering time  

---

## Solution
Designed and implemented an **agentic AI system** that autonomously investigates bugs and generates structured RCA reports.

The system:
- Reads bug details from Azure DevOps  
- Searches and analyzes the codebase  
- Identifies root causes with evidence  
- Generates structured RCA reports with code references  
- Posts results back to the bug tracking system  

All **without human intervention**.

---

## Key Capabilities

- Multi-turn reasoning (up to 25 steps per investigation)
- Dynamic tool usage (MCP-based architecture)
- Intelligent code search with relevance ranking
- Multi-strategy file retrieval (fallback-driven)
- Evidence-based RCA with real code snippets
- CI/CD automation with GitHub Actions
- Production-grade guardrails (timeouts, limits, fallbacks)

---

The system follows an **agentic loop**:
Ingest Bug → Extract Context → Search Code → Retrieve Files
→ Analyze Root Cause → Generate RCA → Post to ADO


### Core Components

| Component | Responsibility |
|----------|----------------|
| `plc_rca_agent.py` | Orchestrates agent loop, reasoning, and tool execution |
| `plc_rca_tools.py` | Toolset (bug fetch, code search, file read, OCR, report generation) |
| `plc_bug_fetcher.py` | Azure DevOps integration with retry logic |
| `mcp_server.py` | JSON-RPC server for tool execution (Model Context Protocol) |
| `plc_github_mcp.py` | MCP-based GitHub integration |
| GitHub Actions | End-to-end automation pipeline |

---

## Agentic Reasoning Loop

The system uses a structured reasoning cycle:

**Plan → Act → Observe → Reflect**

- Decides which tools to use dynamically  
- Adapts investigation path based on findings  
- Iterates until sufficient evidence is gathered  

This is **not a single prompt call** — it is a **true autonomous system**.

---

## Intelligent Code Search

- Combines:
  - Local repository traversal (parallelized)
  - GitHub Code Search API
- Custom relevance scoring:
  - File type prioritization (.cs, .ts, etc.)
  - Keyword and phrase matching
- Deduplication + result fusion

---

## Multi-Strategy File Retrieval

Fallback-driven retrieval ensures high success rate:

1. Direct path resolution  
2. Prefix-based path reconstruction  
3. Recursive filename search  
4. GitHub API fallback  
5. Content-based search  

---

## RCA Output (Structured & Actionable)

Each RCA includes:

- Exact file, method, and line references  
- Evidence (real code snippets)  
- Failure mechanics (data flow, conditions, severity)  
- Minimal patch (proposed fix)  

Output is generated in **structured HTML format** for direct consumption.

---

## ⚙️ CI/CD Automation

End-to-end automation via GitHub Actions:

1. Trigger via webhook or manual input  
2. Fetch bug details (with retries)  
3. Execute agent pipeline  
4. Generate RCA report  
5. Post back to Azure DevOps  
6. Store artifacts (logs, reports)

---

## Production Guardrails

To ensure reliability:

- Max tool calls and execution limits  
- Hard timeout enforcement  
- Context size management  
- Forced completion fallback  
- Graceful shutdown handling  
- Fallback RCA generation  

---

## Impact

| Metric | Before | After |
|------|--------|-------|
| Time per RCA | 2–4 hours | 3–5 minutes |
| Developer effort | Manual investigation | Fully automated |
| Consistency | Variable | Structured & reproducible |
| Coverage | Selective | Every approved bug |

---

##  Tech Stack

- **Language:** Python 3.12  
- **AI Engine:** Claude (Anthropic)  
- **Protocol:** Model Context Protocol (MCP)  
- **Integrations:** Azure DevOps REST API, GitHub API  
- **CI/CD:** GitHub Actions  
- **OCR:** Tesseract (pytesseract, Pillow)  
- **Logging & Resilience:** structlog, backoff  

---

## Key Design Decisions

- **MCP for tool orchestration** → standard, extensible, clean separation  
- **Multi-strategy retrieval** → improves accuracy in large codebases  
- **Structured output format** → ensures actionable results  
- **Guardrails-first design** → critical for production reliability  

---

## Future Enhancements

- Confidence scoring for RCA accuracy  
- Auto-generated fix PRs  
- Multi-repository support  
- Feedback loop for continuous improvement  

---

## Key Takeaway

Agentic AI is not just about generating answers —  
it’s about building **autonomous systems that perform real engineering work**.

This project demonstrates how AI can:
- Reduce engineering toil  
- Improve consistency  
- Scale expertise across teams  

---

## Let's Connect
If you're working on agentic systems, developer productivity, or enterprise AI platforms — would love to exchange ideas.
