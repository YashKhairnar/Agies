# main.py
import streamlit as st
from agent import app, AgentState
from tools import get_sentry_issues
import os

st.title("🪲 BugHunter Agent ")

# GitHub Repository Input
st.subheader("📦 GitHub Repository")
repo_url = os.getenv("GITHUB_REPO", "")

# Normalize repo URL format
if repo_url:
    if not repo_url.startswith("http"):
        if "/" in repo_url:
            repo_url = f"https://github.com/{repo_url}"
        else:
            st.warning("Not monitoring a github repository")
            repo_url = ""
    st.info(f"Monitoring: {repo_url}")


# Fetch and display Sentry issues
st.subheader("📋 Sentry Issues")

if st.button("🔄 Fetch Sentry Issues"):
    org_slug = os.getenv("SENTRY_ORG_SLUG")
    project_slug = os.getenv("SENTRY_PROJECT_SLUG")
    
    if not org_slug:
        st.error("Please set SENTRY_ORG_SLUG environment variable")
    else:
        with st.spinner("Fetching issues from Sentry..."):
            try:
                issues = get_sentry_issues(organization_slug=org_slug, project_slug=project_slug if project_slug else None)
                st.session_state.sentry_issues = issues
                st.success(f"Found {len(issues)} issues")
            except Exception as e:
                st.error(f"Error fetching issues: {str(e)}")
                st.session_state.sentry_issues = []

# Display issues if available
error_id = None
if "sentry_issues" in st.session_state and st.session_state.sentry_issues:
    st.subheader("Select an Issue to Investigate")
    
    # Create a list of issue titles for selection
    issue_options = {}
    for issue in st.session_state.sentry_issues[:50]:  # Limit to first 50
        title = issue.get("title", "Unknown Issue")
        issue_id_val = issue.get("id", "")
        short_id = issue.get("shortId", "")
        count = issue.get("count", 0)
        last_seen = issue.get("lastSeen", "")
        display_text = f"{short_id}: {title[:60]}... ({count} events)" if len(title) > 60 else f"{short_id}: {title} ({count} events)"
        issue_options[display_text] = issue_id_val
    
    selected_issue = st.selectbox("Choose an issue:", options=list(issue_options.keys()))
    error_id = issue_options[selected_issue] if selected_issue else None
    
    # Show issue details
    if selected_issue:
        selected_issue_data = next((i for i in st.session_state.sentry_issues if i.get("id") == error_id), None)
        if selected_issue_data:
            with st.expander("Issue Details"):
                st.json(selected_issue_data)

st.divider()

if st.button("Start Investigation") and error_id:
    if not repo_url:
        st.error("Please enter a GitHub repository URL before starting the investigation.")
        st.stop()
    
    # Get the full issue data for the selected error
    selected_issue_data = None
    if "sentry_issues" in st.session_state and st.session_state.sentry_issues:
        selected_issue_data = next((i for i in st.session_state.sentry_issues if i.get("id") == error_id), None)
    
    # Get detailed Sentry error data
    from tools import get_sentry_error
    try:
        sentry_error_data = get_sentry_error(error_id)
    except Exception as e:
        st.error(f"Failed to fetch Sentry error details: {str(e)}")
        st.stop()
        sentry_error_data = {}
    
    config = {"configurable": {"thread_id": "demo"}}
    initial = {
        "error_id": error_id, 
        "repo_url": repo_url,
        "sentry_data": sentry_error_data,  # Include full Sentry error data
        "messages": [], 
        "needs_approval": False,
        "relevant_files": {}  # Will be populated by sentry_analysis_node
    }

    for output in app.stream(initial, config, stream_mode="updates"):
        if "messages" in output:
            for m in output["messages"]:
                st.write(m)
        
        # Display relevant files found by browser_use
        if "sentry_analysis" in output and "relevant_files" in output["sentry_analysis"]:
            files_data = output["sentry_analysis"]["relevant_files"]
            if files_data and files_data.get("files"):
                st.subheader("🔍 Error-causing Files Found by Browser Use")
                st.write(files_data.get("summary", ""))
                for file_info in files_data.get("files", []):
                    with st.expander(f"📄 {file_info.get('path', 'Unknown')}"):
                        st.write(f"**Reason:** {file_info.get('reason', 'N/A')}")
                        if file_info.get('line_number'):
                            st.write(f"**Line Number:** {file_info.get('line_number')}")

        # Display proposed fix from Gemini
        if "propose_fix" in output and "proposed_fix" in output["propose_fix"]:
            st.subheader("🔧 Proposed Fix by Anthropic")
            proposed_fix = output["propose_fix"]["proposed_fix"]
            st.markdown(proposed_fix)
            
            if output["propose_fix"].get("needs_approval"):
                st.info("⏳ Fix is being tested in Daytona sandbox...")
        
        # Display test results from running fixed code
        if "daytona" in output and "reproduction_steps" in output["daytona"]:
            test_results = output["daytona"]["reproduction_steps"]
            st.subheader("🧪 Test Results (Fixed Code Execution)")
            if "✅" in test_results:
                st.success("Fix verified successfully!")
            elif "❌" in test_results:
                st.error("Fix verification failed")
            st.code(test_results, language="text")
            
            # Approval buttons - these will trigger PR creation
            col1, col2 = st.columns(2)
            
            if col1.button("✅ Approve & Create Draft PR", key="approve_btn"):
                # Update the state to mark as approved and continue
                st.info("Creating PR...")
                
                # Get current state and update it
                current_state = app.get_state(config)
                if current_state and current_state.values:
                    # Update needs_approval to False to proceed
                    updated_state = {**current_state.values, "needs_approval": False}
                    # Continue the graph execution from approval node
                    for pr_output in app.stream(updated_state, config, stream_mode="updates"):
                        if "create_pr" in pr_output:
                            pr_data = pr_output["create_pr"]
                            if pr_data.get("final_pr_url"):
                                st.success(f"✅ PR created: {pr_data['final_pr_url']}")
                                st.markdown(f"[View PR on GitHub]({pr_data['final_pr_url']})")
                                
                                if pr_data.get("messages"):
                                    for msg in pr_data["messages"]:
                                        st.write(msg)
                            else:
                                # Show error messages
                                if pr_data.get("messages"):
                                    for msg in pr_data["messages"]:
                                        if "❌" in msg or "Failed" in msg:
                                            st.error(msg)
                                        else:
                                            st.warning(msg)
                            break
                else:
                    st.error("Could not retrieve current state. Please restart the workflow.")
                
            if col2.button("❌ Reject", key="reject_btn"):
                st.warning("Fix rejected. Workflow stopped.")
                st.stop()
        