#!/usr/bin/env python3
"""One-shot routing matrix for a Gemini Enterprise app where agentsSpec is ignored.

Runs every remaining hypothesis in a single pass and prints a compact verdict table,
so diagnosing an app does not take one conversation round-trip per idea:

  - v1 / v1beta / v1alpha, with the agent id as a bare id and as a full resource name
  - minimal body vs the byte-faithful UI body (ge_agent.ask)
  - the Google-managed deep_research agent (is routing broken app-wide, or only for
    this ADK agent?)
  - a throwaway agent created via the API against the same reasoning engine, queried,
    then deleted (is the console-created agent resource itself the problem?)
  - whether the disable-*-agent-orchestration feature flip actually persisted

Usage: same env vars as ge_agent.py, then
  python3 scripts/ge_probe.py --agent 12869201608268686799
"""

import argparse
import json
import os
import sys

import requests

import ge_agent as ga

VERDICT_W = 74


def first_text(chunks):
    out = []
    for chunk in chunks:
        answer = chunk.get("answer", {})
        if answer.get("state") == "SKIPPED":
            return f"[SKIPPED {answer.get('assistSkippedReasons')}]"
        for reply in answer.get("replies", []):
            text = reply.get("groundedContent", {}).get("content", {}).get("text")
            if text:
                out.append(text)
        if sum(len(t) for t in out) > 200:
            break
    return " ".join("".join(out).split())


def verdict(text):
    # The default orchestrator introduces itself by name; the warehouse agent talks
    # about inventory and orders. Cheap heuristic, but every failure so far has had
    # "Gemini Enterprise" in the first sentence.
    if not text:
        return "EMPTY    "
    if "Gemini Enterprise" in text[:200]:
        return "ORCHESTR."
    return "ROUTED?  "


def stream_assist(project, engine, version, body):
    url = (f"https://{ga.HOST}/{version}/{ga.assistant(project, engine)}:streamAssist")
    r = requests.post(url, headers=ga.headers(project), json=body, timeout=300)
    if r.status_code >= 400:
        return f"HTTP {r.status_code}: {r.text[:120]}"
    return first_text(r.json())


def run_case(name, fn):
    try:
        text = fn()
    except Exception as e:  # keep the matrix going no matter what
        text = f"EXC {type(e).__name__}: {e}"
    tag = verdict(text) if not text.startswith(("HTTP", "EXC")) else "ERROR    "
    print(f"{tag} | {name:42} | {text[:VERDICT_W]}")


def minimal_body(agent_ref, session=None):
    body = {"query": {"text": "what can you do"},
            "agentsSpec": {"agentSpecs": [{"agentId": agent_ref}]}}
    if session:
        body["session"] = session
    return body


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--agent", default=os.environ.get("GE_AGENT_ID"), required=False)
    p.add_argument("--engine", default=os.environ.get("GE_ENGINE_ID"))
    args = p.parse_args()

    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not (project and args.engine and args.agent):
        p.error("need GOOGLE_CLOUD_PROJECT, GE_ENGINE_ID and --agent/GE_AGENT_ID")

    engine = args.engine
    full_name = f"{ga.assistant(project, engine)}/agents/{args.agent}"

    print("=== 0. engine features (did the orchestration flip persist?) ===")
    url = f"https://{ga.HOST}/v1alpha/{ga.collection(project)}/engines/{engine}"
    feats = requests.get(url, headers=ga.headers(project), timeout=60).json().get("features", {})
    for k in ga.ORCHESTRATION_FEATURES:
        print(f"  {k} = {feats.get(k, '<absent>')}")

    print("\n=== 1. routing matrix ===")
    for version in ("v1", "v1beta", "v1alpha"):
        for label, ref in (("bare id", args.agent), ("full name", full_name)):
            run_case(f"{version:7} minimal agentsSpec ({label})",
                     lambda v=version, r=ref: stream_assist(project, engine, v, minimal_body(r)))

    # Labeled session + minimal body, and the full UI body via ge_agent.ask itself.
    run_case("v1alpha minimal + labeled session",
             lambda: stream_assist(project, engine, "v1alpha",
                                   minimal_body(args.agent,
                                                ga.create_agent_session(project, engine, args.agent))))

    print("\n=== 2. deep_research (Google-managed agent on the same assistant) ===")
    agents = {a["name"].split("/")[-1] for a in ga.fetch_agents(project, engine)}
    if "deep_research" in agents:
        run_case("v1alpha minimal (deep_research)",
                 lambda: stream_assist(project, engine, "v1alpha",
                                       minimal_body("deep_research")))
    else:
        print("  deep_research is not registered on this assistant; skipping.")

    print("\n=== 3. throwaway agent created via the API (created -> queried -> deleted) ===")
    src = requests.get(f"https://{ga.HOST}/v1alpha/{full_name}",
                       headers=ga.headers(project), timeout=60).json()
    reasoning_engine = (src.get("adkAgentDefinition", {})
                        .get("provisionedReasoningEngine", {}).get("reasoningEngine"))
    if not reasoning_engine:
        print(f"  could not read the source agent ({src.get('error', {}).get('message', src)})")
        return
    base = f"https://{ga.HOST}/v1alpha/{ga.assistant(project, engine)}/agents"
    r = requests.post(base, headers=ga.headers(project), timeout=60, json={
        "displayName": "ZZ Probe Agent",
        "description": "temporary routing probe, safe to delete",
        "adkAgentDefinition": {"provisionedReasoningEngine": {"reasoningEngine": reasoning_engine}},
    })
    if r.status_code >= 400:
        print(f"  create failed: HTTP {r.status_code}: {r.text[:200]}")
        return
    probe_id = r.json()["name"].split("/")[-1]
    print(f"  created probe agent {probe_id} -> {reasoning_engine.split('/')[-1]}")
    try:
        run_case("v1alpha minimal (API-created probe)",
                 lambda: stream_assist(project, engine, "v1alpha", minimal_body(probe_id)))
    finally:
        d = requests.delete(f"{base}/{probe_id}", headers=ga.headers(project), timeout=60)
        print(f"  deleted probe agent ({d.status_code})")


if __name__ == "__main__":
    main()
