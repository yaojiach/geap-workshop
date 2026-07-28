#!/usr/bin/env python3
"""Call the deployed ADK warehouse agent through a Gemini Enterprise app.

Section 4 Option C reaches the Agent Engine directly (`interact_adk_agent.py`). Once the
same reasoning engine is registered as an agent inside a Gemini Enterprise app, it can
also be reached through the public Discovery Engine API, which is what the Gemini
Enterprise web UI itself talks to.

The UI posts a large internal payload to `:streamAssist` (configId,
experimentIdsForLogging, additionalParams, ...). Almost none of it is required --
`agentsSpec.agentSpecs[].agentId` is what routes the turn to a specific agent, and it is
accepted on v1 (GA), v1beta and v1alpha alike.

  export GOOGLE_CLOUD_PROJECT=your-project
  export GE_ENGINE_ID=gemini-enterprise-xxxxxxxx_xxxxxxxxxxxxx
  python3 scripts/ge_agent.py --list-apps                     # find GE_ENGINE_ID
  python3 scripts/ge_agent.py --list                          # find the agent ID
  python3 scripts/ge_agent.py --agent AGENT_ID "List all items in the warehouse inventory."
"""

import argparse
import json
import os
import subprocess
import sys

import requests

LOCATION = os.environ.get("GE_LOCATION", "global")
API_VERSION = "v1"

HOST = ("discoveryengine.googleapis.com" if LOCATION == "global"
        else f"{LOCATION}-discoveryengine.googleapis.com")


def collection(project):
    return f"projects/{project}/locations/{LOCATION}/collections/default_collection"


def assistant(project, engine):
    return f"{collection(project)}/engines/{engine}/assistants/default_assistant"


def headers():
    token = subprocess.check_output(["gcloud", "auth", "print-access-token"], text=True).strip()
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def list_apps(project):
    url = f"https://{HOST}/{API_VERSION}/{collection(project)}/engines?pageSize=100"
    r = requests.get(url, headers=headers(), timeout=60)
    r.raise_for_status()
    for e in r.json().get("engines", []):
        print(f"{e['name'].split('/')[-1]:>45}  {e.get('solutionType','-'):32}  {e.get('displayName')}")


def list_agents(project, engine):
    # The agents collection is only exposed on v1alpha.
    url = f"https://{HOST}/v1alpha/{assistant(project, engine)}/agents?pageSize=100"
    r = requests.get(url, headers=headers(), timeout=60)
    r.raise_for_status()
    for a in r.json().get("agents", []):
        kind = next((k for k in a if k.endswith("AgentDefinition")), "-")
        engine_ref = ""
        if kind == "adkAgentDefinition":
            engine_ref = (a[kind].get("provisionedReasoningEngine", {})
                          .get("reasoningEngine", "")).split("/")[-1]
        print(f"{a['name'].split('/')[-1]:>22}  {a.get('state','-'):8}  {kind:24}  "
              f"{a.get('displayName')}{f'  -> reasoningEngine {engine_ref}' if engine_ref else ''}")


def ask(project, engine, text, agent_id=None, session=None, show_tools=False):
    body = {"query": {"text": text}}
    if agent_id:
        body["agentsSpec"] = {"agentSpecs": [{"agentId": agent_id}]}
    if session:
        body["session"] = session

    url = f"https://{HOST}/{API_VERSION}/{assistant(project, engine)}:streamAssist"
    r = requests.post(url, headers=headers(), json=body, timeout=300)
    if r.status_code >= 400:
        print(f"HTTP {r.status_code}: {r.text}", file=sys.stderr)
        r.raise_for_status()

    out_session = session
    for chunk in r.json():
        # A session id is minted on the first turn and echoed back in sessionInfo;
        # multi-turn is just feeding that value back in via --session.
        out_session = chunk.get("sessionInfo", {}).get("session", out_session)
        answer = chunk.get("answer", {})
        if answer.get("state") == "SKIPPED":
            print(f"[skipped: {answer.get('assistSkippedReasons')}]")
        for reply in answer.get("replies", []):
            content = reply.get("groundedContent", {}).get("content", {})
            if content.get("text"):
                print(content["text"], end="", flush=True)
            elif show_tools and content.get("executableCode"):
                print(f"\n  [tool call] {json.dumps(content['executableCode'])[:300]}")
            elif show_tools and content.get("codeExecutionResult"):
                print(f"\n  [tool result] {json.dumps(content['codeExecutionResult'])[:300]}")
    print()
    return out_session


def main():
    p = argparse.ArgumentParser(description="Query a Gemini Enterprise agent via Discovery Engine.")
    p.add_argument("query", nargs="?")
    p.add_argument("--list-apps", action="store_true", help="list Gemini Enterprise apps in the project")
    p.add_argument("--list", action="store_true", help="list agents on this app's assistant")
    p.add_argument("--agent", default=os.environ.get("GE_AGENT_ID"),
                   help="agent id (omit to let the default orchestrator route the turn)")
    p.add_argument("--engine", default=os.environ.get("GE_ENGINE_ID"), help="Gemini Enterprise app/engine id")
    p.add_argument("--session", help="existing session resource name, for multi-turn")
    p.add_argument("--show-tools", action="store_true", help="also print tool call/result chunks")
    args = p.parse_args()

    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        p.error("GOOGLE_CLOUD_PROJECT environment variable is not set.")

    if args.list_apps:
        list_apps(project)
        return

    if not args.engine:
        p.error("set GE_ENGINE_ID or pass --engine (use --list-apps to find it).")

    if args.list:
        list_agents(project, args.engine)
    elif args.query:
        session = ask(project, args.engine, args.query, args.agent, args.session, args.show_tools)
        print(f"\nsession: {session}")
    else:
        p.error("give a query, --list or --list-apps")


if __name__ == "__main__":
    main()
