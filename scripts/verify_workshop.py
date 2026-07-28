import os
import re
import sys
import json
import time
import uuid
import argparse
import asyncio
from google import genai
import google.genai.types as genai_types
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

# How long to wait for a single background interaction before giving up. Without a bound
# the poll loop below spins forever whenever the managed agent stalls.
INTERACTION_TIMEOUT_SECONDS = int(os.environ.get("INTERACTION_TIMEOUT_SECONDS", "300"))

def print_banner(text):
    print("\n" + "=" * 60)
    print(f" {text.upper()}")
    print("=" * 60)

def save_session_trajectory(session_id: str, trajectory: dict):
    sessions_dir = "sessions"
    os.makedirs(sessions_dir, exist_ok=True)
    filepath = os.path.join(sessions_dir, f"{session_id}.json")
    with open(filepath, "w") as f:
        json.dump(trajectory, f, indent=2)
    return filepath

async def run_local_verification(project, mcp_url):
    session_id = f"local-verify-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    print(f"Initializing Gemini Client for Local Agent (Project: {project}, Session ID: {session_id})...")
    client = genai.Client(vertexai=True, project=project, location="global")

    headers = {}
    if mcp_url.startswith("https://") and ".run.app" in mcp_url:
        try:
            import subprocess
            cmd = ["gcloud", "auth", "print-identity-token"]
            token = subprocess.check_output(cmd, text=True).strip()
            headers["Authorization"] = f"Bearer {token}"
        except Exception as e:
            print(f"Warning: Could not fetch OIDC token: {e}")

    if mcp_url.endswith("/sse"):
        mcp_url = mcp_url[:-4]
    if not mcp_url.endswith("/mcp"):
        mcp_url = f"{mcp_url}/mcp"

    print(f"Connecting to MCP Streamable HTTP server at {mcp_url}...")
    async with streamablehttp_client(url=mcp_url, headers=headers) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            
            tools_result = await session.list_tools()
            func_declarations = []
            for tool in tools_result.tools:
                fd = genai_types.FunctionDeclaration(
                    name=tool.name,
                    description=tool.description,
                    parameters=tool.inputSchema
                )
                func_declarations.append(fd)

            gemini_tools = [genai_types.Tool(function_declarations=func_declarations)]
            system_instruction = (
                "You are a warehouse management assistant. Your task is to help the user manage inventory "
                "and orders in the warehouse. Use the tools provided by the warehouse-db MCP server to query stock, "
                "update stock, and create orders. Always summarize your action results clearly for the user. "
                "You must NEVER update stock level to fulfill an order if stock is insufficient; reject the order instead."
            )

            history = []
            session_trajectory = {
                "session_id": session_id,
                "agent_type": "local_emulation",
                "project_id": project,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "turns": []
            }
            turn_count = 0

            async def converse_local(prompt):
                nonlocal turn_count
                turn_count += 1
                print(f"\nUser: {prompt}")
                history.append(
                    genai_types.Content(
                        role="user",
                        parts=[genai_types.Part.from_text(text=prompt)]
                    )
                )

                turn_record = {
                    "turn_index": turn_count,
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "user_input": prompt,
                    "steps": [{"step_type": "user_input", "content": prompt}]
                }

                step_num = 1
                while True:
                    response = client.models.generate_content(
                        model="gemini-3.5-flash",
                        contents=history,
                        config=genai_types.GenerateContentConfig(
                            tools=gemini_tools,
                            system_instruction=system_instruction
                        )
                    )

                    if response.text:
                        print(f"Agent: {response.text}")
                        history.append(response.candidates[0].content)
                        turn_record["steps"].append({"step_type": "model_output", "content": response.text})
                        break

                    if response.function_calls:
                        history.append(response.candidates[0].content)
                        response_parts = []
                        for call in response.function_calls:
                            print(f"\n[Step {step_num}] Model requested tool call: '{call.name}'")
                            print(f"  Arguments: {json.dumps(dict(call.args))}")
                            turn_record["steps"].append({
                                "step_type": "function_call",
                                "tool_name": call.name,
                                "arguments": dict(call.args)
                            })
                            
                            mcp_result = await session.call_tool(call.name, arguments=dict(call.args))
                            content_texts = [item.text for item in mcp_result.content if hasattr(item, "text")]
                            result_text = "\n".join(content_texts)
                            print(f"  MCP Result <- Tool: {call.name}")
                            print(f"  Result: {result_text}")
                            
                            turn_record["steps"].append({
                                "step_type": "function_response",
                                "tool_name": call.name,
                                "result": result_text
                            })

                            response_parts.append(
                                genai_types.Part.from_function_response(
                                    name=call.name,
                                    response={"result": result_text}
                                )
                            )
                        history.append(
                            genai_types.Content(
                                role="user",
                                parts=response_parts
                            )
                        )
                        step_num += 1
                    else:
                        break

                session_trajectory["turns"].append(turn_record)
                save_session_trajectory(session_id, session_trajectory)

            # Action 1: List inventory
            print_banner("1. Listing Initial Inventory")
            await converse_local("List all items in the warehouse inventory.")

            # Action 2: Check current stock of Antigravity Boots (ID 3)
            print_banner("2. Querying Stock of Product ID 3")
            await converse_local("Check the current stock level of Antigravity Boots (Product ID 3).")

            # Action 3: Try to place an order that exceeds stock
            print_banner("3. Attempting Invalid Order (Excessive Stock)")
            await converse_local("Place an order for 1000 Antigravity Boots for customer 'Verification Test'.")

            # Action 4: Place a valid order
            print_banner("4. Placing Valid Order (5 Units)")
            await converse_local("Place an order for 5 Antigravity Boots (Product ID 3) for customer 'Verification Test'.")

            # Action 5: Verify stock has decreased
            print_banner("5. Verifying Stock Decreased")
            await converse_local("Check the stock level of Antigravity Boots again to make sure it went down.")

            # Action 6: Session & Trajectory Log Output
            print_banner("6. Session Trajectory Traversal")
            log_file = save_session_trajectory(session_id, session_trajectory)
            print(f"Session trajectory saved to: {log_file}")
            print(f"Total conversation turns in session '{session_id}': {len(session_trajectory['turns'])}")

            print_banner("Verification flow completed successfully!")


def run_remote_verification(project, agent_id):
    print(f"Initializing Gemini Client for Remote Agent (Project: {project}, Location: global)...")
    client = genai.Client(vertexai=True, project=project, location="global", http_options={"timeout": 1200000})

    env_id = "remote"
    prev_interaction_id = None
    print(f"Session environment initialized: {env_id}")

    def converse(prompt):
        nonlocal env_id, prev_interaction_id
        print(f"\nUser: {prompt}")
        interaction = client.interactions.create(
            agent=f"projects/{project}/locations/global/agents/{agent_id}",
            environment=env_id,
            previous_interaction_id=prev_interaction_id,
            input=prompt,
            background=True
        )
        start = time.time()
        while True:
            interaction = client.interactions.get(id=interaction.id)
            status = (interaction.status or "").lower()
            elapsed = time.time() - start
            print(f"[{elapsed:.0f}s] Status: {status}   ", end="\r", flush=True)
            if status in ["succeeded", "failed", "cancelled", "completed", "requires_action"]:
                print()
                break
            if elapsed > INTERACTION_TIMEOUT_SECONDS:
                print()
                raise TimeoutError(
                    f"Interaction {interaction.id} was still '{status}' after "
                    f"{INTERACTION_TIMEOUT_SECONDS}s. Managed agents currently stall when a "
                    f"prompt requires an MCP tool call -- see the troubleshooting notes in "
                    f"README.md. Inspect it with:\n"
                    f"  python3 scripts/observe_agent.py --interaction_id {interaction.id}"
                )
            time.sleep(2)
        print()
        print(f"Agent: {interaction.output_text.strip() if interaction.output_text else ''}")
        print(f"[Interaction ID: {interaction.id}]")

        if interaction.environment_id:
            env_id = interaction.environment_id
        prev_interaction_id = interaction.id
        return interaction.id

    print_banner("1. Listing Initial Inventory")
    converse("List all items in the warehouse inventory.")

    print_banner("2. Querying Stock of Product ID 3")
    converse("Check the current stock level of Antigravity Boots (Product ID 3).")

    print_banner("3. Attempting Invalid Order (Excessive Stock)")
    converse("Place an order for 1000 Antigravity Boots for customer 'Verification Test'.")

    print_banner("4. Placing Valid Order (5 Units)")
    last_interaction_id = converse("Place an order for 5 Antigravity Boots for customer 'Verification Test'.")

    print_banner("5. Verifying Stock Decreased")
    converse("Check the stock level of Antigravity Boots again to make sure it went down.")

    print_banner("6. Retrieving Step Trace (Observability)")
    print(f"Fetching trace for interaction: {last_interaction_id}...")
    try:
        interaction = client.interactions.get(id=last_interaction_id)
        steps = getattr(interaction, "steps", []) or []
        for i, step in enumerate(steps):
            step_type = getattr(step, "type", "unknown")
            print(f"\n[Step {i+1}] Type: {step_type}")
            if step_type == "thought":
                for part in getattr(step, "content", []):
                    if hasattr(part, "text") and part.text:
                        print(f"  Thought: {part.text.strip()}")
            elif step_type == "mcp_server_tool_call":
                tool_name = getattr(step, "name", "unknown")
                server_name = getattr(step, "server_name", "unknown")
                args = getattr(step, "arguments", {})
                print(f"  MCP Call -> Server: {server_name}, Tool: {tool_name}")
                print(f"  Arguments: {json.dumps(args)}")
            elif step_type == "mcp_server_tool_result":
                tool_name = getattr(step, "name", "unknown")
                result = getattr(step, "result", "")
                print(f"  MCP Result <- Tool: {tool_name}")
                print(f"  Result: {result}")
            elif step_type == "model_output":
                for part in getattr(step, "content", []):
                    if hasattr(part, "text") and part.text:
                        print(f"  Response: {part.text.strip()}")
    except Exception as e:
        print(f"Failed to fetch interaction trace: {e}")

    print_banner("Verification flow completed successfully!")


def run_adk_verification(project, engine_name):
    print_banner("Running ADK Reasoning Engine Verification")
    import adk_common

    print(f"Initializing SDK & loading Reasoning Engine: {engine_name}...")
    agent = adk_common.load_agent_engine(project, engine_name)
    user_id = adk_common.DEFAULT_USER_ID

    # The session ID must be issued by Agent Engine -- a locally invented ID has no
    # server-side history, so the agent would forget everything between turns.
    session_id = adk_common.create_session(agent, user_id)
    print(f"Active Session ID: {session_id} (user: {user_id})")

    def converse(prompt):
        print(f"\nUser: {prompt}")
        response = adk_common.stream_turn(
            agent, prompt, session_id=session_id, user_id=user_id,
            on_tool_call=lambda name, args: print(
                f"  [Tool Call] {name} with args: {json.dumps(args, default=str)}"),
            on_tool_response=lambda name, result: print(
                f"  [Tool Result] {name}: {json.dumps(result, default=str)[:300]}"),
        )
        print(f"Agent: {response}")

    print_banner("1. Listing Initial Inventory")
    converse("List all items in the warehouse inventory.")

    print_banner("2. Querying Stock of Product ID 3")
    converse("Check the current stock level of Antigravity Boots (Product ID 3).")

    print_banner("3. Attempting Invalid Order (Excessive Stock)")
    converse("Place an order for 1000 Antigravity Boots for customer 'Verification Test'.")

    print_banner("4. Placing Valid Order (5 Units)")
    converse("Place an order for 5 Antigravity Boots (Product ID 3) for customer 'Verification Test'.")

    print_banner("5. Verifying Stock Decreased")
    converse("Check the stock level of Antigravity Boots (Product ID 3) again to make sure it went down.")

    print_banner("6. Session Trajectory Traversal via API")
    print(f"Fetching session trajectory for session '{session_id}' via agent API...")
    try:
        session = agent.get_session(user_id=user_id, session_id=session_id)
        trajectory = adk_common.session_to_trajectory(session, session_id)
        log_file = save_session_trajectory(session_id, trajectory)
        print(json.dumps(trajectory, indent=2, default=str))
        print(f"\nSession trajectory saved to: {log_file}")
        print(f"Total conversation turns in session '{session_id}': {len(trajectory['turns'])}")
    except Exception as e:
        print(f"Failed to fetch session trajectory via API: {e}")

    print_banner("Verification flow completed successfully!")


def main():
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        print("Error: GOOGLE_CLOUD_PROJECT environment variable is not set.")
        sys.exit(1)

    prefix = os.environ.get("GEAP_PREFIX")
    if not prefix:
        print("Error: GEAP_PREFIX environment variable is not set.")
        sys.exit(1)
    mcp_url = os.environ.get("MCP_SERVER_URL")
    agent_id = f"{prefix}-warehouse-manager"

    parser = argparse.ArgumentParser(description="Verify the GEAP Warehouse Management Agent.")
    parser.add_argument("--remote", action="store_true", help="Run verification using the remote cloud managed agent.")
    parser.add_argument("--adk", type=str, help="Run verification using the deployed ADK Reasoning Engine (pass resource name or ID).")
    args = parser.parse_args()

    if args.adk:
        engine_name = args.adk
        if not engine_name.startswith("projects/"):
            engine_name = f"projects/{project}/locations/us-central1/reasoningEngines/{args.adk}"
        run_adk_verification(project, engine_name)
    elif args.remote:
        try:
            run_remote_verification(project, agent_id)
        except TimeoutError as e:
            print(f"\nERROR: {e}")
            sys.exit(1)
    else:
        if not mcp_url:
            print("Error: MCP_SERVER_URL environment variable is not set.")
            sys.exit(1)
        print("Running in LOCAL agent emulation mode...")
        asyncio.run(run_local_verification(project, mcp_url))

if __name__ == "__main__":
    main()
