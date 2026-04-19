# AgenticRCA

This directory contains the Agentic Root Cause Analysis (RCA) agent and supporting tools for automated bug triage and analysis, designed for integration with Model Context Protocol (MCP) and Azure DevOps/GitHub workflows.

## Purpose
AgenticRCA provides an automated, agent-driven approach to root cause analysis of software bugs. It fetches bug details from Azure DevOps, analyzes them using AI and custom logic, and generates detailed RCA reports. The tools here are designed to be used both as standalone scripts and as part of CI/CD pipelines.

## Directory Contents

- **mcp_server.py**: Launches the MCP server for agentic RCA operations.
- **plc_bug_fetcher.py**: Fetches bug details from Azure DevOps and stores them locally for analysis.
- **plc_github_mcp.py**: Integrates with GitHub and MCP for workflow automation and data exchange.
- **plc_rca_agent.py**: The main entry point for running the RCA agent, orchestrating the analysis process.
- **plc_rca_prompt.txt**: Contains prompt templates and instructions for the AI agent.
- **plc_rca_tools.py**: Utility functions and helpers for bug fetching, analysis, and report generation.
- **requirements.txt**: Python dependencies required for all scripts in this directory.
- **__pycache__/**: Compiled Python bytecode files (auto-generated).

## Key Features
- Automated bug data fetching from Azure DevOps
- AI-driven root cause analysis and report generation
- Integration with GitHub Actions and MCP
- OCR and image processing support (via Pillow and pytesseract)
- Modular design for easy extension and customization

## Usage

### Standalone
You can run the RCA agent or supporting scripts directly:

```sh
python plc_rca_agent.py
```

Or start the MCP server:

```sh
python mcp_server.py
```

### As Part of CI/CD
This directory is designed to be used in automated workflows (see `.github/workflows/plc_rca_workflow.yml`). The workflow will:
- Set up the Python environment
- Install dependencies from `requirements.txt`
- Fetch bug details
- Run the RCA agent
- Generate and upload RCA reports

## Requirements
- Python 3.12+
- Azure DevOps and GitHub access tokens (for bug fetching and posting results)
- Tesseract OCR (for image-based bug data)

## Contributing
- Ensure all new scripts are documented and tested
- Update `requirements.txt` if new dependencies are added
- Follow the code style and structure of existing files

## License
This directory is part of the Pathlock PLC project. See the root repository for license details.
