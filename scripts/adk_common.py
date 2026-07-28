"""Shared helpers for talking to a deployed ADK Agent Engine (Section 4, Option C).

The deployed AdkApp exposes `create_session`, `list_sessions`, `get_session` and
`stream_query` as registered operations. It does NOT expose a `query` method, and only
`vertexai.agent_engines.get()` binds the streaming operations onto the returned object --
`vertexai.preview.reasoning_engines.ReasoningEngine` does not.
"""

import os
import time

# Managed sessions are scoped to a user, so every workshop script uses the same one by
# default. Override with ADK_USER_ID if you want to keep separate session histories.
DEFAULT_USER_ID = os.environ.get("ADK_USER_ID", "workshop-user")


def resolve_engine_name(project: str, name_or_id: str, location: str = None) -> str:
    """Accepts either a bare engine ID or a full resource name."""
    location = location or os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    if name_or_id.startswith("projects/"):
        return name_or_id
    return f"projects/{project}/locations/{location}/reasoningEngines/{name_or_id}"


def load_agent_engine(project: str, engine_name: str, location: str = None):
    """Loads a deployed Agent Engine with its streaming operations bound."""
    import vertexai
    from vertexai import agent_engines

    location = location or os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    vertexai.init(project=project, location=location)
    return agent_engines.get(resolve_engine_name(project, engine_name, location))


def create_session(agent, user_id: str = DEFAULT_USER_ID) -> str:
    """Creates a managed session and returns its server-generated ID."""
    session = agent.create_session(user_id=user_id)
    return session.get("id") or session.get("session_id")


def latest_session_id(agent, user_id: str = DEFAULT_USER_ID) -> str:
    """Returns the most recently updated managed session ID, or None."""
    sessions = agent.list_sessions(user_id=user_id).get("sessions", [])
    if not sessions:
        return None
    sessions.sort(key=lambda s: _field(s, "last_update_time", "lastUpdateTime") or 0, reverse=True)
    return sessions[0].get("id")


def _field(part: dict, snake: str, camel: str):
    """Reads a Part field regardless of casing, treating explicit nulls as absent.

    stream_query() yields snake_case keys while get_session() yields camelCase, and both
    include every Part field with a null value -- so `"text" in part` is always true and
    cannot be used to tell a text part from a function call.
    """
    return part.get(snake) or part.get(camel)


def stream_turn(agent, message: str, session_id: str, user_id: str = DEFAULT_USER_ID,
                on_tool_call=None, on_tool_response=None) -> str:
    """Runs one turn against the remote agent and returns the final text response."""
    text_parts = []
    for event in agent.stream_query(user_id=user_id, session_id=session_id, message=message):
        if not isinstance(event, dict):
            continue
        for part in (event.get("content") or {}).get("parts", []):
            call = _field(part, "function_call", "functionCall")
            resp = _field(part, "function_response", "functionResponse")
            text = part.get("text")
            if call:
                if on_tool_call:
                    on_tool_call(call.get("name"), call.get("args", {}))
            elif resp:
                if on_tool_response:
                    on_tool_response(resp.get("name"), resp.get("response", {}))
            elif text:
                text_parts.append(text)
    return "".join(text_parts).strip()


def session_to_trajectory(session: dict, session_id: str) -> dict:
    """Converts managed session events into the same shape the local scripts write out."""
    trajectory = {
        "session_id": session_id,
        "agent_type": "adk_agent_engine",
        "created_at": _format_ts(_field(session, "last_update_time", "lastUpdateTime")),
        "turns": [],
    }
    turn = None
    for event in session.get("events", []):
        content = event.get("content") or {}
        role = content.get("role", "")
        timestamp = _format_ts(event.get("timestamp"))
        for part in content.get("parts", []):
            call = _field(part, "function_call", "functionCall")
            resp = _field(part, "function_response", "functionResponse")
            text = part.get("text")
            if call:
                if turn:
                    turn["steps"].append({
                        "step_type": "function_call",
                        "tool_name": call.get("name"),
                        "arguments": call.get("args") or {},
                    })
            elif resp:
                if turn:
                    turn["steps"].append({
                        "step_type": "function_response",
                        "tool_name": resp.get("name"),
                        "result": resp.get("response") or {},
                    })
            elif text and role == "user":
                # A user text part starts a new turn; tool results also carry role "user"
                # but arrive as functionResponse parts, so they stay inside the open turn.
                turn = {
                    "turn_index": len(trajectory["turns"]) + 1,
                    "timestamp": timestamp,
                    "user_input": text,
                    "steps": [{"step_type": "user_input", "content": text}],
                }
                trajectory["turns"].append(turn)
            elif text and turn:
                turn["steps"].append({"step_type": "model_output", "content": text})
    return trajectory


def _format_ts(value) -> str:
    if not value:
        return ""
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(value)))
    except (TypeError, ValueError):
        return str(value)
