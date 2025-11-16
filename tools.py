# tools.py
import os
import requests
from sentry_sdk.api import capture_message
import json
import asyncio
import base64
from browser_use import Agent, ChatGoogle
from daytona import Daytona, DaytonaConfig

async def _run_browser_task(task: str) -> str:
    # Set up LLM for the agent (using Google Gemini)
    llm = ChatGoogle(
        model="gemini-2.5-flash",
        api_key=os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    )
    # Create agent with the task
    agent = Agent(
        task=task,
        llm=llm,
    )
    # Run the agent
    result = await agent.run()
    # Extract the final result
    output = result.final_result()
    return output if output else ""

def search_github(repo: str, query: str) -> list[dict]:
    task = f"""
    You are a precise GitHub researcher. Do exactly this and nothing else:
    1. Go to https://github.com/{repo}/issues
    2. In the search box type: {query} is:open
    3. Hit Enter
    4. From the issue list, extract the first 5 issues
    5. Output ONLY valid JSON in this exact format, no extra text:
    [
      {{"title": "Exact issue title", "url": "https://github.com/{repo}/issues/123"}}
    ]
    If login wall appears, stop and return empty list [].
    """
    # Run the async task synchronously
    output = asyncio.run(_run_browser_task(task))
    
    try:
        issues = json.loads(output)
        return issues[:5]
    except:
        print("Browser Use JSON parse failed, returning empty")
        return []

def find_files_from_sentry_issue(repo_url: str, sentry_issue_data: dict) -> dict:
    """Use browser_use to find the files that cause the issue from the GitHub repo"""
    
    # Extract relevant information from Sentry issue
    # sentry_issue_data can be either the issue summary or the detailed event
    error_type = sentry_issue_data.get("type") or sentry_issue_data.get("title", "Unknown Error")
    error_message = ""
    
    # Try different paths for error message
    if "metadata" in sentry_issue_data:
        error_message = sentry_issue_data.get("metadata", {}).get("value", "")
    if not error_message and "message" in sentry_issue_data:
        error_message = sentry_issue_data.get("message", "")
    
    exception_data = sentry_issue_data.get("exception", {})
    
    # Get stack trace information if available
    stack_trace = ""
    if exception_data and "values" in exception_data and len(exception_data["values"]) > 0:
        first_exception = exception_data["values"][0]
        stack_trace = json.dumps(first_exception.get("stacktrace", {}), indent=2)
    elif "stacktrace" in sentry_issue_data:
        stack_trace = json.dumps(sentry_issue_data.get("stacktrace", {}), indent=2)
    
    # Extract repo name from URL
    repo_name = repo_url.replace("https://github.com/", "").replace(".git", "")
    
    task = f"""
    You are a code detective analyzing a Sentry error to find the problematic files in a GitHub repository.
    
    Sentry Error Information:
    - Error Type: {error_type}
    - Error Message: {error_message}
    - Stack Trace: {stack_trace[:1000]}  # Limited to first 1000 chars
    
    Your task:
    1. Go to https://github.com/{repo_name}
    2. Navigate to the repository's code/files section
    3. Based on the error type, message, and stack trace, identify the files that are likely causing this issue
    4. Look for files mentioned in the stack trace (file paths, function names, etc.)
    5. Search the repository for relevant files using GitHub's file finder or search functionality
    6. For each file you find, note:
       - The full file path
       - A brief reason why this file is relevant to the error
       - The line number if mentioned in the stack trace
    
    Output ONLY valid JSON in this exact format, no extra text:
    {{
      "files": [
        {{
          "path": "src/app.py",
          "reason": "Stack trace shows error at line 42 in app.py",
          "line_number": 42
        }},
        {{
          "path": "utils/helper.py",
          "reason": "Function called from app.py is defined here",
          "line_number": null
        }}
      ],
      "summary": "Brief summary of what files are involved and why"
    }}
    
    If you cannot find specific files, return an empty files array but still provide a summary based on the error type.
    """
    
    # Run the async task synchronously
    output = asyncio.run(_run_browser_task(task))
    
    try:
        result = json.loads(output)
        return result
    except json.JSONDecodeError as e:
        print(f"Browser Use JSON parse failed: {e}")
        print(f"Raw output: {output[:500]}")
        # Return a fallback structure
        return {
            "files": [],
            "summary": f"Could not parse browser output. Error: {str(e)}. Raw output: {output[:200]}"
        }


def get_sentry_issues(organization_slug: str = None, project_slug: str = None, limit: int = 50) -> list[dict]:
    """Fetch Sentry issues from an organization/project"""
    org_slug = organization_slug or os.getenv("SENTRY_ORG_SLUG")
    project_slug = project_slug or os.getenv("SENTRY_PROJECT_SLUG")
    
    if not org_slug:
        raise ValueError("Sentry organization slug is required (set SENTRY_ORG_SLUG env var)")
    
    # Build the URL - if project is specified, use project-specific endpoint
    if project_slug:
        url = f"https://sentry.io/api/0/projects/{org_slug}/{project_slug}/issues/"
    else:
        url = f"https://sentry.io/api/0/organizations/{org_slug}/issues/"
    
    headers = {"Authorization": f"Bearer {os.getenv('SENTRY_TOKEN')}"}
    params = {
        "statsPeriod": "14d",  # Last 14 days
        "query": "is:unresolved",  # Only unresolved issues
        "limit": limit
    }
    
    r = requests.get(url, headers=headers, params=params)
    r.raise_for_status()
    return r.json()

def get_sentry_error(error_id: str) -> dict:
    """Get details of a specific Sentry error/issue"""
    url = f"https://sentry.io/api/0/issues/{error_id}/events/"
    headers = {"Authorization": f"Bearer {os.getenv('SENTRY_TOKEN')}"}
    r = requests.get(url, headers=headers)
    r.raise_for_status()
    return r.json()[0]  # latest event

#daytona tools
def create_daytona_workspace_with_fix(repo_url: str, file_to_fix: str = None, fixed_code: str = None, proposed_fix: str = "", branch: str = "main") -> tuple:
    """Create Daytona workspace, apply the fix, and run the code to verify it works"""
    # Configure Daytona client
    config = DaytonaConfig(
        api_key=os.getenv("DAYTONA_API_KEY"),
        api_url=os.getenv("DAYTONA_API_URL", "https://app.daytona.io/api"),
        target=os.getenv("DAYTONA_TARGET", "us")
    )
    daytona = Daytona(config)
    # Create a new sandbox
    sandbox = daytona.create()
    # Wait for sandbox to be ready before cloning
    sandbox.wait_for_sandbox_start(timeout=60)
    # Get the home directory path
    home_dir = sandbox.get_user_home_dir()
    print(f"Sandbox home directory: {home_dir}")
    
    # Extract repo name from URL
    repo_name = repo_url.split('/')[-1].replace('.git', '')
    print(f"Cloning repository: {repo_name}")
    
    # Clone the repository
    sandbox.git.clone(url=repo_url, path=".", branch=branch)
    print("Repository cloned")
    
    # Apply the fix if we have file path and fixed code
    if file_to_fix and fixed_code:
        print(f"Applying fix to: {file_to_fix}")
        # Write the fixed code to the file using upload_file
        full_file_path = f"{repo_name}/{file_to_fix}"
        # Convert string to bytes for upload
        fixed_code_bytes = fixed_code.encode('utf-8')
        # upload_file takes file as first positional argument, then remote_path
        sandbox.fs.upload_file(fixed_code_bytes, full_file_path)
        print(f"Fixed code written to {full_file_path}")
    
    # Check if requirements.txt exists, then install dependencies and run the application
    print("Checking for requirements.txt...")
    print("Found requirements.txt, installing dependencies...")
    install_cmd = "pip install -r requirements.txt"
    
    print("Running application...")
    response = sandbox.process.exec(
        command=f"{install_cmd} && python app.py",
        cwd=repo_name,
        timeout=120
    )
    
    # Extract output
    stdout_stderr = response.result if response.result else ""
    
    if response.exit_code != 0:
        output_text = f"❌ Application failed with exit code: {response.exit_code}\n\nOutput:\n{stdout_stderr}"
    else:
        output_text = f"✅ Application ran successfully!\n\nOutput:\n{stdout_stderr}" if stdout_stderr else "✅ Application executed successfully (no output)"
    
    print(f"Execution exit code: {response.exit_code}")
    
    # Store workspace_id before deleting sandbox
    workspace_id = sandbox.id
    
    # Clean up
    sandbox.delete()
    
    return (workspace_id, output_text)
    

def create_draft_pr(repo: str, branch: str, title: str, body: str, file_path: str = None, file_content: str = None) -> tuple:
    """Create a draft PR on GitHub without committing any files - just creates a PR with description"""
    github_token = os.getenv('GITHUB_TOKEN')
    if not github_token:
        raise ValueError("GITHUB_TOKEN environment variable must be set")
    
    # Extract owner and repo name from repo string (format: owner/repo or full URL)
    if repo.startswith("https://github.com/"):
        repo = repo.replace("https://github.com/", "").replace(".git", "")
    elif repo.startswith("http://github.com/"):
        repo = repo.replace("http://github.com/", "").replace(".git", "")
    
    # Clean file path - remove backticks and extra whitespace
    if file_path:
        import re
        file_path = file_path.strip().strip('`').strip('"').strip("'")
    
    # Sanitize branch name - remove invalid characters
    import re
    branch = re.sub(r'[^a-zA-Z0-9_/-]', '-', branch)
    branch = branch.strip('/').strip('-')
    if not branch:
        branch = "bugfix/patch"
    
    print(f"Creating PR for repo: {repo}, branch: {branch}")
    print(f"File path: {file_path}, Has content: {bool(file_content)}")
    
    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github.v3+json"
    }
    
    # Step 1: Get the default branch (usually 'main' or 'master')
    repo_response = requests.get(f"https://api.github.com/repos/{repo}", headers=headers)
    
    if repo_response.status_code != 200:
        error_msg = f"Failed to get repo info: {repo_response.status_code} - {repo_response.text}"
        print(error_msg)
        raise Exception(error_msg)
    repo_info = repo_response.json()
    default_branch = repo_info.get("default_branch", "main")
    print(f"Default branch: {default_branch}")
    
    # Step 2: Get the SHA of the default branch
    ref_response = requests.get(
        f"https://api.github.com/repos/{repo}/git/ref/heads/{default_branch}",
        headers=headers
    )
    if ref_response.status_code != 200:
        error_msg = f"Failed to get branch SHA: {ref_response.status_code} - {ref_response.text}"
        print(error_msg)
        raise Exception(error_msg)
    base_sha = ref_response.json()["object"]["sha"]
    print(f"Base SHA: {base_sha[:8]}...")
    
    # Step 3: Create a new branch
    branch_ref = f"refs/heads/{branch}"
    create_branch_response = requests.post(
        f"https://api.github.com/repos/{repo}/git/refs",
        json={
            "ref": branch_ref,
            "sha": base_sha
        },
        headers=headers
    )
    # If branch already exists, that's okay - we'll use it
    if create_branch_response.status_code == 422:
        print(f"Branch {branch} already exists, using existing branch")
        # Branch exists, get its SHA
        existing_branch = requests.get(
            f"https://api.github.com/repos/{repo}/git/ref/heads/{branch}",
            headers=headers
        )
        if existing_branch.status_code == 200:
            base_sha = existing_branch.json()["object"]["sha"]
            print(f"Using existing branch SHA: {base_sha[:8]}...")
        else:
            error_msg = f"Failed to access existing branch: {existing_branch.status_code} - {existing_branch.text}"
            print(error_msg)
            raise Exception(error_msg)
    elif create_branch_response.status_code != 201:
        error_msg = f"Failed to create branch: {create_branch_response.status_code} - {create_branch_response.text}"
        print(error_msg)
        raise Exception(error_msg)
    else:
        print(f"Branch {branch} created successfully")
    
    # Step 4: Create an empty commit to make the branch different from base
    # GitHub requires at least one commit difference to create a PR
    print("Creating empty commit to enable PR creation...")
    try:
        # Get the commit object to get its tree SHA
        commit_obj_response = requests.get(
            f"https://api.github.com/repos/{repo}/git/commits/{base_sha}",
            headers=headers
        )
        if commit_obj_response.status_code == 200:
            commit_obj = commit_obj_response.json()
            tree_sha = commit_obj["tree"]["sha"]
            
            # Create an empty commit (same tree, no file changes)
            commit_data = {
                "message": f"Fix: {title}\n\n{body[:500]}",  # Include title and description in commit message
                "tree": tree_sha,  # Same tree as parent (no changes)
                "parents": [base_sha]
            }
            
            commit_response = requests.post(
                f"https://api.github.com/repos/{repo}/git/commits",
                json=commit_data,
                headers=headers
            )
            
            if commit_response.status_code == 201:
                new_commit_sha = commit_response.json()["sha"]
                # Update branch to point to new commit
                update_ref_response = requests.patch(
                    f"https://api.github.com/repos/{repo}/git/refs/heads/{branch}",
                    json={"sha": new_commit_sha},
                    headers=headers
                )
                if update_ref_response.status_code == 200:
                    print("Empty commit created successfully")
                else:
                    error_msg = f"Failed to update branch ref: {update_ref_response.status_code} - {update_ref_response.text}"
                    print(error_msg)
                    raise Exception(error_msg)
            else:
                error_msg = f"Failed to create empty commit: {commit_response.status_code} - {commit_response.text}"
                print(error_msg)
                raise Exception(error_msg)
        else:
            error_msg = f"Failed to get commit object: {commit_obj_response.status_code} - {commit_obj_response.text}"
            print(error_msg)
            raise Exception(error_msg)
    except Exception as e:
        error_msg = f"Failed to create empty commit: {str(e)}"
        print(error_msg)
        raise Exception(error_msg)
    
    # Step 5: Check if a PR already exists for this branch
    print(f"Checking for existing PR on branch: {branch}")
    existing_prs = requests.get(
        f"https://api.github.com/repos/{repo}/pulls",
        headers=headers,
        params={"head": f"{repo.split('/')[0]}:{branch}", "state": "open"}
    )
    
    if existing_prs.status_code == 200:
        existing_pr_list = existing_prs.json()
        if existing_pr_list:
            existing_pr = existing_pr_list[0]
            pr_url = existing_pr["html_url"]
            pr_number = existing_pr["number"]
            print(f"PR already exists: {pr_url}")
            return pr_url, pr_number
    
    # Step 6: Create the PR (now that branch has a commit different from base)
    # The PR will show no file changes but will have the title and description
    print(f"Creating PR: {title}")
    pr_response = requests.post(
        f"https://api.github.com/repos/{repo}/pulls",
        json={
        "title": title,
        "head": branch,
            "base": default_branch,
        "body": body,
        "draft": True,
        },
        headers=headers
    )
    if pr_response.status_code == 422:
        # Check if it's because PR already exists
        error_data = pr_response.json()
        if "already exists" in str(error_data).lower():
            # Try to find the existing PR
            existing_prs = requests.get(
                f"https://api.github.com/repos/{repo}/pulls",
                headers=headers,
                params={"head": f"{repo.split('/')[0]}:{branch}", "state": "all"}
            )
            if existing_prs.status_code == 200:
                existing_pr_list = existing_prs.json()
                if existing_pr_list:
                    existing_pr = existing_pr_list[0]
                    pr_url = existing_pr["html_url"]
                    pr_number = existing_pr["number"]
                    print(f"PR already exists (found via error): {pr_url}")
                    return pr_url, pr_number
        error_msg = f"Failed to create PR: {pr_response.status_code} - {pr_response.text}"
        print(error_msg)
        raise Exception(error_msg)
    elif pr_response.status_code != 201:
        error_msg = f"Failed to create PR: {pr_response.status_code} - {pr_response.text}"
        print(error_msg)
        raise Exception(error_msg)
    pr_data = pr_response.json()
    pr_url = pr_data["html_url"]
    pr_number = pr_data["number"]
    print(f"PR created successfully: {pr_url}")

    
    return pr_url, pr_number
