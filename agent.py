# agent.py
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from langchain_google_genai import ChatGoogleGenerativeAI
from tools import *
import json
import os
from typing import TypedDict

llm = ChatGoogleGenerativeAI(
    model="gemini-2.5-flash", 
    temperature=0, 
    google_api_key=os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
)

class AgentState(TypedDict):
    error_id: str
    sentry_data: dict
    repo_url: str
    workspace_id: str
    reproduction_steps: str
    proposed_fix: str
    github_issues: list
    final_pr_url: str
    pr_number: int
    messages: list
    needs_approval: bool
    relevant_files: dict  # Files found by browser_use analysis

def sentry_analysis_node(state):
    """Analyze Sentry issue and find relevant files using browser_use"""
    repo_url = state["repo_url"]
    sentry_data = state["sentry_data"]
    
    # Use browser_use to find files related to the error
    files_analysis = find_files_from_sentry_issue(repo_url, sentry_data)
    
    return {
        "relevant_files": files_analysis,
        "messages": [f"Found {len(files_analysis.get('files', []))} relevant files using browser analysis"]
    }

def daytona_node(state):
    """Run the fixed code in Daytona to verify the fix works"""
    repo_url = state["repo_url"]
    proposed_fix = state.get("proposed_fix", "")
    
    # Extract the file path and fixed code from the proposed fix
    # This is a simple extraction - in production you might want more robust parsing
    file_to_fix = None
    fixed_code = None
    
    # Try to extract file path and code from the proposed fix
    lines = proposed_fix.split("\n")
    in_code_block = False
    code_lines = []
    
    for i, line in enumerate(lines):
        # Extract file path
        if "**File to Fix:**" in line:
            file_to_fix = line.split("**File to Fix:**")[-1].strip()
        
        # Extract code block (handle both ```python and ```)
        if "```" in line:
            if not in_code_block:
                # Start of code block
                in_code_block = True
                code_lines = []
            else:
                # End of code block
                in_code_block = False
                if code_lines:
                    fixed_code = "\n".join(code_lines)
                    break
        elif in_code_block:
            code_lines.append(line)
    
    # Create workspace and apply the fix
    workspace_id, test_output = create_daytona_workspace_with_fix(
        repo_url, 
        file_to_fix=file_to_fix,
        fixed_code=fixed_code,
        proposed_fix=proposed_fix
    )
    
    return {
        "workspace_id": workspace_id,
        "reproduction_steps": test_output,  # This now contains test results
        "messages": [f"Fixed code executed in Daytona. Result: {test_output[:100]}..."]
    }

def research_node(state):
    query = state["sentry_data"]["exception"]["values"][0]["type"]
    issues = asyncio.run(search_github(state["repo_url"].split("github.com/")[1], query))  # ← just add asyncio.run
    return {"github_issues": issues}

def propose_fix_node(state):
    """Use Gemini to analyze the error and relevant files to propose a fix"""
    
    # Extract relevant files and reasons from browser_use analysis
    relevant_files = state.get('relevant_files', {})
    files_list = relevant_files.get('files', [])
    files_summary = relevant_files.get('summary', '')
    
    # Build files context
    files_context = ""
    if files_list:
        files_context = "Relevant files identified:\n"
        for file_info in files_list:
            file_path = file_info.get('path', 'Unknown')
            reason = file_info.get('reason', 'N/A')
            line_num = file_info.get('line_number')
            files_context += f"- {file_path}"
            if line_num:
                files_context += f" (line {line_num})"
            files_context += f": {reason}\n"
    
    # Extract Sentry error details
    sentry_data = state.get('sentry_data', {})
    error_type = sentry_data.get('type') or sentry_data.get('title', 'Unknown Error')
    error_message = ""
    if 'metadata' in sentry_data:
        error_message = sentry_data.get('metadata', {}).get('value', '')
    if not error_message and 'message' in sentry_data:
        error_message = sentry_data.get('message', '')
    
    prompt = f"""You are a senior software engineer tasked with fixing a bug. Analyze the error and propose a fix.

SENTRY ERROR INFORMATION:
- Error Type: {error_type}
- Error Message: {error_message}

{files_context}

FILES ANALYSIS SUMMARY:
{files_summary if files_summary else 'No additional summary available'}

TASK:
1. Analyze the Sentry error to understand what went wrong
2. Consider the relevant files identified and why they're related to the error
3. Propose a fix that addresses the root cause
4. Show the complete fixed code for the affected file(s)
5. Provide a clear PR title and description

OUTPUT FORMAT:
Provide your response in the following format:

**File to Fix:** [file path]

**Problem:**
[Brief description of what's wrong]

**Solution:**
[Explanation of the fix]

**Fixed Code:**
```python
[Complete fixed code for the file]
```

**PR Title:**
[Clear, descriptive PR title]

**PR Description:**
[Detailed description of the fix and why it solves the issue]
"""
    
    response = llm.invoke(prompt)
    return {"proposed_fix": response.content, "needs_approval": True}

def human_approval_node(state):
    """Wait for human approval - in Streamlit this is handled by UI buttons"""
    # The actual approval is handled in the UI
    # This node just passes through - the conditional edge checks needs_approval
    # When user clicks approve, we'll update needs_approval to False
    return state

def extract_pr_info(proposed_fix: str) -> dict:
    """Extract PR title, description, file path, and code from proposed fix"""
    lines = proposed_fix.split("\n")
    
    pr_title = None
    pr_description = None
    file_to_fix = None
    fixed_code = None
    
    in_code_block = False
    code_lines = []
    description_lines = []
    
    for i, line in enumerate(lines):
        # Extract PR title
        if "**PR Title:**" in line:
            pr_title = line.split("**PR Title:**")[-1].strip()
        
        # Extract file path - clean it up (remove backticks, quotes, etc.)
        if "**File to Fix:**" in line:
            file_to_fix = line.split("**File to Fix:**")[-1].strip()
            # Remove backticks, quotes, and extra whitespace
            import re
            file_to_fix = re.sub(r'[`"\']', '', file_to_fix).strip()
        
        # Extract PR description
        if "**PR Description:**" in line:
            # Description starts on next line
            for j in range(i + 1, len(lines)):
                if lines[j].startswith("**") and ":" in lines[j]:
                    break
                description_lines.append(lines[j])
            pr_description = "\n".join(description_lines).strip()
        
        # Extract code block
        if "```" in line:
            if not in_code_block:
                in_code_block = True
                code_lines = []
            else:
                in_code_block = False
                if code_lines:
                    fixed_code = "\n".join(code_lines)
        elif in_code_block:
            code_lines.append(line)
    
    return {
        "title": pr_title or "Fix: Bug resolution",
        "description": pr_description or proposed_fix,
        "file_path": file_to_fix,
        "code": fixed_code
    }

def create_pr_node(state):
    """Create a GitHub PR with the fix and trigger CodeRabbit review"""
    repo_url = state.get("repo_url", "")
    proposed_fix = state.get("proposed_fix", "")
    
    # Extract PR information from proposed fix
    pr_info = extract_pr_info(proposed_fix)
    print(pr_info)
    
    # Generate branch name from error ID or use default
    error_id = state.get("error_id", "bugfix")
    branch_name = f"bugfix/{error_id[:20]}"  # Limit branch name length
    print(branch_name)
    # Create the PR
    try:
        print(f"PR Info extracted - Title: {pr_info['title']}, File: {pr_info['file_path']}, Has code: {bool(pr_info['code'])}")
        
        pr_url, pr_number = create_draft_pr(
            repo=repo_url,
            branch=branch_name,
            title=pr_info["title"],
            body=pr_info["description"],
            file_path=pr_info["file_path"],
            file_content=pr_info["code"]
        )
        
        return {
            "final_pr_url": pr_url,
            "pr_number": pr_number,
            "messages": [
                f"✅ Draft PR created: {pr_url}"
            ]
        }
        
    except Exception as e:
        error_msg = f"❌ Failed to create PR: {str(e)}"
        print(error_msg)
        import traceback
        traceback.print_exc()
        return {
            "final_pr_url": "",
            "messages": [error_msg]
        }

graph = StateGraph(AgentState)
graph.add_node("sentry_analysis", sentry_analysis_node)
graph.add_node("daytona", daytona_node)
graph.add_node("propose_fix", propose_fix_node)
graph.add_node("approval", human_approval_node)
graph.add_node("create_pr", create_pr_node)

graph.add_edge("sentry_analysis", "propose_fix")
graph.add_edge("propose_fix", "daytona")
graph.add_edge("daytona", "approval")  # Go to approval after testing
graph.add_edge("approval",'create_pr')
graph.add_edge("create_pr", END)

graph.set_entry_point("sentry_analysis")
app = graph.compile(checkpointer=MemorySaver())