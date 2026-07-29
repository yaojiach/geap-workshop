import argparse
import json
import os
import sys

import adk_common

def main():
    parser = argparse.ArgumentParser(description="Interactive CLI Client for Deployed ADK Agent")
    parser.add_argument("--resource_name", help="Reasoning Engine resource name")
    parser.add_argument("--query", help="Single query to run non-interactively")
    parser.add_argument("--session_id", help="Reuse an existing managed session instead of creating one")
    args = parser.parse_args()

    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        print("Error: GOOGLE_CLOUD_PROJECT environment variable is not set.")
        sys.exit(1)
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    user_id = adk_common.DEFAULT_USER_ID

    engine_name = args.resource_name or os.environ.get("REASONING_ENGINE_NAME")
    if not engine_name:
        print("Error: REASONING_ENGINE_NAME environment variable or --resource_name is required.")
        sys.exit(1)

    print(f"Initializing Vertex AI SDK (Project: {project}, Location: {location})...")
    print(f"Connecting to Reasoning Engine: {engine_name}...")
    try:
        agent = adk_common.load_agent_engine(project, engine_name, location)
    except Exception as e:
        print(f"Error loading Reasoning Engine: {e}", file=sys.stderr)
        sys.exit(1)

    # Sessions are created and stored by Agent Engine; the ID has to come from the service.
    # Making one up client-side means every turn starts from an empty history.
    session_id = args.session_id or os.environ.get("SESSION_ID")
    if session_id:
        print(f"Reusing managed session: {session_id}")
    else:
        session_id = adk_common.create_session(agent, user_id)
        print(f"Created managed session: {session_id}")

    def show_tool_response(name, result):
        # The MCP tools return their failures as an ordinary string with the full
        # traceback in it (see _execute_mcp_tool in adk_agent.py), so dropping the
        # result here turns a precise remote stack trace into the model's vague
        # "I was unable to retrieve the inventory list due to an internal error."
        text = result if isinstance(result, str) else str(result)
        if text.startswith("Error"):
            print(f"  [Tool Response] {name} FAILED:\n{text}")
        else:
            first = text.strip().splitlines()[0] if text.strip() else ""
            print(f"  [Tool Response] {name}  {first[:100]}"
                  f"{' ...' if len(text) > 100 else ''}")

    def execute_query(user_input: str) -> str:
        return adk_common.stream_turn(
            agent,
            user_input,
            session_id=session_id,
            user_id=user_id,
            on_tool_call=lambda name, tool_args: print(f"  [Tool Call] {name}({tool_args})"),
            on_tool_response=show_tool_response,
        )

    if args.query:
        print(f"\nQuerying Agent: '{args.query}'...")
        res = execute_query(args.query)
        print(f"\nAgent Response:\n{res}")
        print(f"\n[Session ID: {session_id}]")
        return

    print("\n" + "="*60)
    print("Welcome to the Vertex AI Reasoning Engine ADK Agent CLI Client!")
    print(f"Active Session ID: {session_id} (user: {user_id})")
    print("="*60)
    print("This interactive console allows you to chat directly with your remote Python-packaged agent.")
    print("Type 'exit' or 'quit' to end the conversation.")
    print("Type 'session' or 'trajectory' to view the full session trajectory via API.")
    print("="*60 + "\n")

    while True:
        try:
            user_input = input("\nYou: ")
            if not user_input.strip():
                continue
            if user_input.strip().lower() in ["exit", "quit"]:
                print("Goodbye!")
                break

            if user_input.strip().lower() in ["session", "trajectory"]:
                print(f"\nFetching Session Trajectory via API for Session: {session_id}...")
                try:
                    sess_data = agent.get_session(user_id=user_id, session_id=session_id)
                    print(json.dumps(sess_data, indent=2, default=str))
                except Exception as e:
                    print(f"Could not fetch session data: {e}")
                continue

            print("\nThinking (Remote Agent executing reasoning loop)...")
            response = execute_query(user_input)
            print(f"\nAgent:\n{response}")

        except KeyboardInterrupt:
            print("\nGoodbye!")
            break
        except EOFError:
            break
        except Exception as e:
            print(f"\nError: {e}")
            break

if __name__ == "__main__":
    main()
