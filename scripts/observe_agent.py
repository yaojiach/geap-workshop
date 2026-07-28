import os
import sys
import json
import glob
import argparse
from google import genai

def print_trajectory(data: dict, title: str):
    print("\n" + "="*60)
    print(f"{title} (ID: {data.get('session_id')})")
    print(f"Created At: {data.get('created_at')}")
    print("="*60)

    turns = data.get("turns", [])
    print(f"Total Turns: {len(turns)}")

    for turn in turns:
        print("\n" + "-"*60)
        print(f"Turn #{turn.get('turn_index')} [{turn.get('timestamp')}] User: '{turn.get('user_input')}'")
        print("-" * 60)
        for i, step in enumerate(turn.get("steps", [])):
            stype = step.get("step_type")
            if stype == "user_input":
                print(f"  [Step {i+1}] User Input: {step.get('content')}")
            elif stype == "thought":
                print(f"  [Step {i+1}] Thought: {step.get('content')}")
            elif stype == "function_call":
                print(f"  [Step {i+1}] Function Call: '{step.get('tool_name')}' with args: {json.dumps(step.get('arguments'), default=str)}")
            elif stype == "function_response":
                print(f"  [Step {i+1}] Function Result ({step.get('tool_name')}): {step.get('result')}")
            elif stype == "model_output":
                print(f"  [Step {i+1}] Model Output: {step.get('content')}")
            else:
                print(f"  [Step {i+1}] {stype}: {step}")
    print("\n" + "="*60)


def observe_local_session(session_id: str = None) -> bool:
    session_dirs = ["sessions", "/tmp/sessions"]
    session_file = None

    if session_id:
        for sdir in session_dirs:
            candidate = os.path.join(sdir, f"{session_id}.json")
            if os.path.exists(candidate):
                session_file = candidate
                break
    else:
        all_files = []
        for sdir in session_dirs:
            if os.path.exists(sdir):
                all_files.extend(glob.glob(os.path.join(sdir, "*.json")))
        if all_files:
            all_files.sort(key=os.path.getmtime, reverse=True)
            session_file = all_files[0]

    if not session_file or not os.path.exists(session_file):
        return False

    print(f"Loading local session trajectory: {session_file}")
    with open(session_file, "r") as f:
        data = json.load(f)

    print_trajectory(data, "LOCAL SESSION TRAJECTORY LOG")
    return True


def fetch_managed_gcp_session_events(engine_name: str, session_id: str):
    """Fetches session events directly from GCP Managed Agent Engine Sessions REST API."""
    import urllib.request
    import google.auth
    from google.auth.transport.requests import Request

    try:
        creds, _ = google.auth.default()
        creds.refresh(Request())
        token = creds.token

        if not engine_name.startswith("projects/"):
            project = os.environ.get("GOOGLE_CLOUD_PROJECT")
            engine_name = f"projects/{project}/locations/us-central1/reasoningEngines/{engine_name}"

        # If session_id is simple ID, construct full path
        if not "/sessions/" in session_id:
            url = f"https://us-central1-aiplatform.googleapis.com/v1beta1/{engine_name}/sessions/{session_id}/events"
        else:
            url = f"https://us-central1-aiplatform.googleapis.com/v1beta1/{session_id}/events"

        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {token}"},
            method="GET"
        )
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode())
            print("\n" + "="*60)
            print(f"GCP MANAGED SESSION EVENTS (Session: {session_id})")
            print("="*60)
            events = data.get("sessionEvents", [])
            print(f"Total Session Events Recorded: {len(events)}")
            for i, evt in enumerate(events):
                author = evt.get("author", "unknown")
                timestamp = evt.get("timestamp", "")
                parts = evt.get("content", {}).get("parts", [])
                # Every Part field is present with a null value, so summarise whichever one
                # is actually populated rather than assuming the event carries text.
                summary = []
                for p in parts:
                    if p.get("functionCall"):
                        call = p["functionCall"]
                        summary.append(f"[tool call] {call.get('name')}({json.dumps(call.get('args') or {})})")
                    elif p.get("functionResponse"):
                        summary.append(f"[tool result] {p['functionResponse'].get('name')}")
                    elif p.get("text"):
                        summary.append(p["text"])
                print(f"  [Event {i+1}] [{timestamp}] {author.upper()}: {' '.join(summary)}")
            print("="*60)
            return True
    except Exception as e:
        print(f"Managed Session REST API query note: {e}")
        return False


def observe_adk_session(project: str, engine_name: str, session_id: str = None):
    import adk_common

    engine_name = adk_common.resolve_engine_name(project, engine_name)
    print(f"Connecting to Reasoning Engine API: {engine_name}...")
    agent = adk_common.load_agent_engine(project, engine_name)
    user_id = adk_common.DEFAULT_USER_ID

    # Session IDs are issued by Agent Engine, so rather than demanding one we can look up
    # the most recent session belonging to this user.
    if not session_id:
        session_id = adk_common.latest_session_id(agent, user_id)
        if not session_id:
            print(f"No managed sessions found for user '{user_id}' on {engine_name}.")
            print("Run scripts/interact_adk_agent.py or verify_workshop.py --adk first.")
            return
        print(f"No --session_id given; using the most recent session: {session_id}")

    print(f"\nFetching Managed Session Events via GCP REST API for Session ID: {session_id}...")
    fetch_managed_gcp_session_events(engine_name, session_id)

    print(f"\nFetching Session Trajectory via SDK for Session ID: {session_id}...")
    try:
        session = agent.get_session(user_id=user_id, session_id=session_id)
    except Exception as e:
        print(f"Error fetching session trajectory: {e}")
        return

    print_trajectory(adk_common.session_to_trajectory(session, session_id),
                     "REASONING ENGINE SESSION TRAJECTORY")


def observe_interaction(project: str, interaction_id: str):
    print(f"Initializing Gemini Client (Project: {project}, Location: global)...")
    client = genai.Client(vertexai=True, project=project, location="global")

    print(f"Fetching details for interaction: {interaction_id}...")
    try:
        interaction = client.interactions.get(id=interaction_id)
    except Exception as e:
        print(f"Failed to fetch interaction: {e}")
        sys.exit(1)

    print("\n" + "="*60)
    print(f"INTERACTION DETAILS (ID: {interaction.id})")
    print("="*60)
    print(f"Agent ID:    {interaction.agent}")
    print(f"Environment: {interaction.environment_id}")
    print(f"Status:      {interaction.status}")
    print("="*60)

    steps = getattr(interaction, "steps", []) or []
    for i, step in enumerate(steps):
        step_type = getattr(step, "type", "unknown")
        print(f"\n[Step {i+1}] Type: {step_type}")
        if step_type == "thought":
            for part in (getattr(step, "content", []) or []):
                if hasattr(part, "text") and part.text:
                    print(f"  Thought: {part.text.strip()}")
        elif step_type == "model_output":
            for part in (getattr(step, "content", []) or []):
                if hasattr(part, "text") and part.text:
                    print(f"  Output: {part.text.strip()}")
        elif step_type == "user_input":
            for part in (getattr(step, "content", []) or []):
                if hasattr(part, "text") and part.text:
                    print(f"  Input: {part.text.strip()}")
        elif step_type == "mcp_server_tool_call":
            print(f"  Tool Call: '{getattr(step, 'name', 'unknown')}' on '{getattr(step, 'server_name', 'unknown')}'")
            print(f"  Args: {json.dumps(getattr(step, 'arguments', {}))}")
        elif step_type == "mcp_server_tool_result":
            print(f"  Tool Result ('{getattr(step, 'name', 'unknown')}'): {getattr(step, 'result', '')}")


def main():
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    parser = argparse.ArgumentParser(description="View Agent Session & Trajectory Logs.")
    parser.add_argument("--session_id", type=str, help="Session ID to inspect.")
    parser.add_argument("--adk", type=str, help="Deployed ADK Reasoning Engine name or ID.")
    parser.add_argument("--interaction_id", type=str, help="Managed agent interaction ID.")
    parser.add_argument("--local", action="store_true", help="Force viewing local session trajectory.")
    args = parser.parse_args()

    if not project and not args.local:
        print("Error: GOOGLE_CLOUD_PROJECT environment variable is not set.")
        sys.exit(1)

    if args.interaction_id:
        observe_interaction(project, args.interaction_id)
    elif args.adk:
        observe_adk_session(project, args.adk, session_id=args.session_id)
    elif args.local:
        if not observe_local_session(session_id=args.session_id):
            print(f"Local session trajectory not found for session_id: {args.session_id or 'latest'}")
    else:
        if not observe_local_session(session_id=args.session_id):
            if args.session_id and project:
                try:
                    from google.cloud import aiplatform
                    from vertexai.preview import reasoning_engines
                    aiplatform.init(project=project)
                    engines = reasoning_engines.ReasoningEngine.list()
                    if engines:
                        engine_name = engines[0].resource_name
                        print(f"Local session file not found. Auto-detected deployed Reasoning Engine: {engine_name}")
                        observe_adk_session(project, engine_name, session_id=args.session_id)
                        return
                except Exception as e:
                    print(f"Auto-detecting Reasoning Engine note: {e}")
            print(f"No session trajectory found for session_id: {args.session_id or 'latest'}")

if __name__ == "__main__":
    main()
