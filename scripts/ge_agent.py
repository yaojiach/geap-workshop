#!/usr/bin/env python3
"""Call the deployed ADK warehouse agent through a Gemini Enterprise app.

Section 4 Option C reaches the Agent Engine directly (`interact_adk_agent.py`). Once the
same reasoning engine is registered as an agent inside a Gemini Enterprise app, it can
also be reached through the public Discovery Engine API, which is what the Gemini
Enterprise web UI itself talks to.

The UI posts a large internal payload to `:streamAssist` (configId,
experimentIdsForLogging, additionalParams, ...). Most of it is noise, but two fields are
load-bearing and neither appears in the published discovery document:

  agentsSpec.agentSpecs[].agentId  routes the turn to a specific agent (works on v1,
                                   v1beta and v1alpha)
  assistSkippingMode=REQUEST_ASSIST  stops the assistant from dropping anything its
                                   classifier reads as chit-chat; v1alpha only, so that
                                   is the default version here

Without the second one, `ge_agent.py "hi"` comes back empty with
NON_ASSIST_SEEKING_QUERY_IGNORED while the same greeting is answered in the web UI.

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

# v1alpha, not v1. Several fields the GE web UI sends are missing from the published
# discovery document *and* from the v1/v1beta backends, which reject unknown names with
# HTTP 400 rather than ignoring them. The one that matters here is `assistSkippingMode`:
# without it the assistant drops anything its classifier reads as chit-chat ("hi") with
# NON_ASSIST_SEEKING_QUERY_IGNORED and never reaches the agent at all.
# (`agentsSpec` is undocumented too, but it does work on all three versions.)
API_VERSION = os.environ.get("GE_API_VERSION", "v1alpha")

# v1 and v1beta reject these UI-only fields outright with HTTP 400 "Unknown name".
ALPHA_ONLY_VERSIONS = ("v1alpha",)

DEBUG = bool(os.environ.get("GE_DEBUG"))

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


def fetch_agents(project, engine):
    # The agents collection is only exposed on v1alpha.
    url = f"https://{HOST}/v1alpha/{assistant(project, engine)}/agents?pageSize=100"
    r = requests.get(url, headers=headers(), timeout=60)
    r.raise_for_status()
    return r.json().get("agents", [])


def describe_agent(a):
    kind = next((k for k in a if k.endswith("AgentDefinition")), "-")
    engine_ref = ""
    if kind == "adkAgentDefinition":
        engine_ref = (a[kind].get("provisionedReasoningEngine", {})
                      .get("reasoningEngine", "")).split("/")[-1]
    return (f"{a['name'].split('/')[-1]:>22}  {a.get('state','-'):8}  {kind:24}  "
            f"{a.get('displayName')}{f'  -> reasoningEngine {engine_ref}' if engine_ref else ''}")


def list_agents(project, engine):
    for a in fetch_agents(project, engine):
        print(describe_agent(a))


def describe(project, engine, agent_id=None):
    """Dump the raw engine, assistant and agent resources.

    For diffing against an app where `agentsSpec` routing does work. Nothing in the
    :streamAssist request explains why one app honours an agentId and another answers
    the turn with its default orchestrator, so the difference has to be in the app's
    own configuration -- and much of that configuration is missing from the published
    discovery document, so dump it raw rather than trusting the schema.

    `assistants` is a plural collection: an app with more than one is worth a second
    look, since everything here hardcodes `default_assistant`.
    """
    eng = f"https://{HOST}/v1alpha/{collection(project)}/engines/{engine}"
    targets = [("engine", eng), ("assistants", f"{eng}/assistants?pageSize=100")]
    if agent_id:
        targets.append(("agent", f"https://{HOST}/v1alpha/{assistant(project, engine)}/agents/{agent_id}"))
    for label, url in targets:
        r = requests.get(url, headers=headers(), timeout=60)
        print(f"===== {label} ({r.status_code}) =====")
        print(json.dumps(r.json(), indent=4))


def check_agent(project, engine, agent_id):
    """Fail loudly on an agent id this assistant does not have.

    An unknown agentId is not rejected by :streamAssist -- the turn silently falls
    through to the default orchestrator, which answers in a plausible but generic
    voice ("I don't have direct access to your warehouse inventory system"). That
    reads like a broken agent rather than a bad id, so verify up front.
    """
    agents = fetch_agents(project, engine)
    if any(a["name"].split("/")[-1] == agent_id for a in agents):
        return
    print(f"Agent '{agent_id}' is not registered on this assistant "
          f"(engine {engine}).\nAn unknown agentId would be silently ignored and the "
          f"turn answered by the default orchestrator, so refusing to send it.\n",
          file=sys.stderr)
    if agents:
        print("Available agents:", file=sys.stderr)
        for a in agents:
            print(f"  {describe_agent(a)}", file=sys.stderr)
    else:
        print("This assistant has no agents registered yet.", file=sys.stderr)
    sys.exit(1)


def ask(project, engine, text, agent_id=None, session=None, show_tools=False, raw=False):
    body: dict = {"query": {"text": text}}
    if agent_id:
        body["agentsSpec"] = {"agentSpecs": [{"agentId": agent_id}]}
    if session:
        body["session"] = session
    if API_VERSION in ALPHA_ONLY_VERSIONS:
        # Reproduce the body the web UI posts, field for field. Which of these the
        # backend actually needs varies by app: on one app `agentsSpec` alone routes
        # correctly, on a newer one the same request is answered by the default
        # orchestrator. Rather than bisect that per app, send what the UI sends -- every
        # field below is accepted on v1alpha and none of them changes routing on an app
        # that already works. (`configId`, `additionalParams` and
        # `experimentIdsForLogging` sit *outside* `streamAssistRequest` in the UI's own
        # wrapper, are not part of :streamAssist, and would be rejected as unknown.)
        body["query"] = {"parts": [{"text": text}]}
        body["assistSkippingMode"] = "REQUEST_ASSIST"
        body["toolsSpec"] = {
            "webGroundingSpec": {},
            "toolRegistry": "default_tool_registry",
            "imageGenerationSpec": {},
            "videoGenerationSpec": {},
            "canvasSpec": {},
        }
        body["languageCode"] = os.environ.get("GE_LANGUAGE_CODE", "en-US")
        body["filter"] = ""
        body["fileIds"] = []
        if agent_id:
            body["agentsConfig"] = {"agent": agent_id}
            body["answerGenerationMode"] = "AGENT"

    # Always say who the turn was routed to. An answer in the default orchestrator's
    # voice is indistinguishable from a badly-behaved agent, and "was an agent id
    # actually resolved?" is the first thing you need to know when that happens.
    route = agent_id or "NONE -> default orchestrator"
    print(f"[ge_agent] {API_VERSION}  engine={engine}  agent={route}", file=sys.stderr)
    if DEBUG:
        print(json.dumps(body, indent=2), file=sys.stderr)

    url = f"https://{HOST}/{API_VERSION}/{assistant(project, engine)}:streamAssist"
    r = requests.post(url, headers=headers(), json=body, timeout=300)
    if r.status_code >= 400:
        print(f"HTTP {r.status_code}: {r.text}", file=sys.stderr)
        r.raise_for_status()

    chunks = r.json()
    if raw:
        # The printed text alone cannot tell you whether the agent was invoked and
        # answered like an orchestrator, or never invoked at all. The chunks carry
        # metadata the pretty-printer drops -- assistToken, agent/session info, and
        # any per-reply attribution -- which is the only in-band evidence of routing.
        print(json.dumps(chunks, indent=2), file=sys.stderr)

    out_session = session
    for chunk in chunks:
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
    p.add_argument("--describe", action="store_true",
                   help="dump the raw agent + assistant resources for --agent")
    p.add_argument("--agent", default=os.environ.get("GE_AGENT_ID"),
                   help="agent id (omit to let the default orchestrator route the turn)")
    p.add_argument("--engine", default=os.environ.get("GE_ENGINE_ID"), help="Gemini Enterprise app/engine id")
    p.add_argument("--session", help="existing session resource name, for multi-turn")
    p.add_argument("--show-tools", action="store_true", help="also print tool call/result chunks")
    p.add_argument("--raw", action="store_true", help="dump the full :streamAssist response to stderr")
    p.add_argument("--no-check-agent", action="store_true",
                   help="skip verifying --agent against the assistant's agent list")
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
    elif args.describe:
        describe(project, args.engine, args.agent)
    elif args.query:
        if args.agent and not args.no_check_agent:
            check_agent(project, args.engine, args.agent)
        session = ask(project, args.engine, args.query, args.agent, args.session,
                      args.show_tools, args.raw)
        print(f"\nsession: {session}")
    else:
        p.error("give a query, --list or --list-apps")


if __name__ == "__main__":
    main()
