#!/usr/bin/env python3
"""LangChain Agent on Vertex AI Reasoning Engine.

This script demonstrates how to define, locally test, and deploy a LangChain
agent using Vertex AI Reasoning Engine (ADK).
"""

import argparse
import os
import re
import sys
import vertexai
from vertexai.preview import reasoning_engines


def get_word_length(word: str) -> int:
    """Returns the number of characters in a word.

    Useful for counting letters or length of any given word.
    """
    print(f"[Tool Execution] Calculating length of '{word}'...")
    return len(word)


def main():
    parser = argparse.ArgumentParser(
        description="Run or deploy a sample LangChain agent."
    )
    parser.add_argument(
        "--deploy", action="store_true", help="Deploy the agent to Vertex AI"
    )
    args = parser.parse_args()

    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        print("Error: GOOGLE_CLOUD_PROJECT environment variable is not set.")
        sys.exit(1)

    location = "us-central1"

    if args.deploy:
        bucket = os.environ.get("STAGING_BUCKET")
        if not bucket:
            print("Error: STAGING_BUCKET environment variable is not set.")
            sys.exit(1)
        print(
            f"Initializing Vertex AI (Project: {project}, Location: {location}, Bucket: {bucket})..."
        )
        vertexai.init(project=project, location=location, staging_bucket=bucket)
    else:
        print(f"Initializing Vertex AI (Project: {project}, Location: {location})...")
        vertexai.init(project=project, location=location)

    # 1. Define the LangchainAgent instance
    # By default, it will use langchain-google-vertexai ChatVertexAI model under the hood.
    print("Creating LangChain agent...")
    agent = reasoning_engines.LangchainAgent(
        model="gemini-2.5-flash",
        tools=[get_word_length],
        system_instruction="You are a helpful text-analysis assistant. Use tools when needed.",
        model_kwargs={"temperature": 0.0},
    )

    if not args.deploy:
        # 2. Test locally
        print("\n====================================================")
        # set_up compiles the LangChain graph/agent executor locally
        agent.set_up()
        print("Running local query check...")
        prompt = "How many letters are in the word supercalifragilisticexpialidocious?"
        print(f"User: {prompt}")
        response = agent.query(input=prompt)
        print(f"Agent: {response}")
        print("====================================================")
        print("Local verification succeeded!")

    else:
        # 3. Deploy remotely
        print("\nDeploying LangChain agent to Vertex AI...")
        prefix = os.environ.get("GEAP_PREFIX")
        if not prefix:
            print("Error: GEAP_PREFIX environment variable is not set.")
            sys.exit(1)
        remote_agent = reasoning_engines.ReasoningEngine.create(
            agent,
            requirements=[
                "google-cloud-aiplatform[langchain,reasoningengine]",
                "langchain-google-vertexai",
                "cloudpickle",
                "pydantic",
                # Without these the container resolves an opentelemetry-api that predates
                # `opentelemetry._events`, and Agent Engine's own telemetry_utils fails to
                # import at startup ("failed to start and cannot serve traffic").
                "opentelemetry-api>=1.30",
                "opentelemetry-sdk>=1.30",
            ],
            display_name=f"{prefix}-sample-langchain-agent",
            gcs_dir_name=f"{prefix}-langchain-agent",
            description="A sample LangChain-based agent deployed via Vertex AI Reasoning Engine.",
        )
        print("\n====================================================")
        print("LangChain Agent Deployed Successfully!")
        print(f"Resource Name: {remote_agent.resource_name}")
        print("====================================================")


if __name__ == "__main__":
    main()
