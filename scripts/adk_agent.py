import asyncio
import os
import sys
import time
import json
import httpx
import google.auth
from google.auth.transport.requests import Request
import google.oauth2.id_token
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from google.adk.agents import Agent
from vertexai.agent_engines import AdkApp

def get_mcp_auth_headers(mcp_url: str) -> dict:
    """Helper to fetch Cloud Run OIDC ID Token for MCP authentication."""
    headers = {}
    if not mcp_url:
        return headers
    if "run.app" in mcp_url or "https://" in mcp_url:
        try:
            audience = mcp_url.split("/mcp")[0].split("/sse")[0]
            token = google.oauth2.id_token.fetch_id_token(Request(), audience)
            if token:
                headers["Authorization"] = f"Bearer {token}"
        except Exception:
            try:
                import subprocess
                token = subprocess.check_output(["gcloud", "auth", "print-identity-token"]).decode().strip()
                if token:
                    headers["Authorization"] = f"Bearer {token}"
            except Exception:
                pass
    return headers

async def _execute_mcp_tool(tool_name: str, arguments: dict = None) -> str:
    """Executes a tool on the remote MCP server using streamable HTTP."""
    mcp_url = os.environ.get("MCP_SERVER_URL")
    if not mcp_url:
        return "Error: MCP_SERVER_URL environment variable is not configured."
    
    headers = get_mcp_auth_headers(mcp_url)
    arguments = arguments or {}
    
    try:
        async with httpx.AsyncClient(headers=headers, timeout=30.0) as http_client:
            async with streamable_http_client(mcp_url, http_client=http_client) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    res = await session.call_tool(tool_name, arguments)
                    parts = []
                    for content in res.content:
                        if hasattr(content, "text"):
                            parts.append(content.text)
                    return "\n".join(parts)
    except Exception as e:
        import traceback
        sub_errs = getattr(e, "exceptions", [])
        if sub_errs:
            err_details = "; ".join([f"{type(se).__name__}: {se}" for se in sub_errs])
            return f"Error executing tool '{tool_name}': {type(e).__name__}: {e} -> Sub-exceptions: {err_details}\nTrace:\n{traceback.format_exc()}"
        return f"Error executing tool '{tool_name}': {type(e).__name__}: {e}\nTrace:\n{traceback.format_exc()}"

# ADK Tool Definitions
async def list_inventory() -> str:
    """Lists all products currently in stock in the warehouse inventory, showing product ID, name, description, quantity, and price."""
    return await _execute_mcp_tool("list_inventory", {})

async def get_product_stock(product_id: int) -> str:
    """Retrieves current stock level and details for a specific product by product_id."""
    return await _execute_mcp_tool("get_product_stock", {"product_id": product_id})

async def update_stock(product_id: int, quantity: int) -> str:
    """Updates the stock level for a product directly by product_id."""
    return await _execute_mcp_tool("update_stock", {"product_id": product_id, "quantity": quantity})

async def create_order(customer_name: str, product_id: int, quantity: int) -> str:
    """Creates a new customer order for a product and automatically adjusts inventory stock level."""
    return await _execute_mcp_tool("create_order", {
        "customer_name": customer_name,
        "product_id": product_id,
        "quantity": quantity
    })

def create_warehouse_agent() -> Agent:
    """Creates the root ADK LlmAgent instance following agents-cli best practices."""
    return Agent(
        name="warehouse_adk_agent",
        model="gemini-2.5-flash",
        instruction=(
            "You are a warehouse management assistant. Your task is to help the user manage inventory "
            "and orders in the warehouse. Use the provided tools (list_inventory, get_product_stock, update_stock, create_order) "
            "to query stock, update stock, and process orders. Always summarize your action results clearly for the user. "
            "You must NEVER update stock level using update_stock to satisfy an order; reject the order instead if stock is insufficient. "
            "When asked about product stock or details, refer to previous conversation history or call get_product_stock/list_inventory directly."
        ),
        tools=[list_inventory, get_product_stock, update_stock, create_order]
    )

def create_adk_app() -> AdkApp:
    """Wraps the root ADK agent into an AdkApp instance for local execution & Reasoning Engine deployment."""
    root_agent = create_warehouse_agent()
    return AdkApp(agent=root_agent, enable_tracing=True)

def deploy_agent(project_id: str, location: str, mcp_url: str, staging_bucket: str):
    """Deploys the ADK Agent (AdkApp) to Vertex AI Reasoning Engine."""
    import vertexai
    from google.cloud import aiplatform
    from vertexai import agent_engines

    prefix = os.environ.get("GEAP_PREFIX")
    if not prefix:
        print("Error: GEAP_PREFIX environment variable is not set.")
        sys.exit(1)
    
    try:
        import subprocess
        mcp_service_name = f"{prefix}-warehouse-mcp-server"
        print(f"Granting Cloud Run invoker permissions for service '{mcp_service_name}'...")
        proj_num = subprocess.check_output(["gcloud", "projects", "describe", project_id, "--format", "value(projectNumber)"]).decode().strip()
        members = [
            f"serviceAccount:service-{proj_num}@gcp-sa-aiplatform-re.iam.gserviceaccount.com",
            f"serviceAccount:{proj_num}-compute@developer.gserviceaccount.com"
        ]
        try:
            user_acct = subprocess.check_output(["gcloud", "config", "get-value", "account"]).decode().strip()
            if user_acct:
                members.append(f"user:{user_acct}")
        except Exception:
            pass

        for member in members:
            subprocess.check_call([
                "gcloud", "run", "services", "add-iam-policy-binding", mcp_service_name,
                "--member", member,
                "--role", "roles/run.invoker",
                "--region", location,
                "--project", project_id,
                "--quiet"
            ])
        print("Successfully granted IAM permissions.")
    except Exception as e:
        print(f"Warning: Failed to grant IAM permissions automatically: {e}")

    print(f"Initializing vertexai & aiplatform (Project: {project_id}, Location: {location}, Staging Bucket: {staging_bucket})...")
    vertexai.init(project=project_id, location=location, staging_bucket=staging_bucket)
    aiplatform.init(project=project_id, location=location, staging_bucket=staging_bucket)
    
    adk_app_instance = create_adk_app()
    
    print("Deploying Agent Engine with google-adk Agent framework...")
    try:
        reasoning_engine = agent_engines.create(
            agent_engine=adk_app_instance,
            requirements=[
                # Keep this list in sync with requirements.txt: google-adk 2.x caps
                # opentelemetry-api/sdk at <=1.42.1, so the instrumentation packages must
                # stay on the 0.63b1 line or pip resolves google-adk back down to 1.10.0.
                "google-adk>=2.5.0",
                "google-cloud-aiplatform",
                "google-genai",
                "mcp>=0.1.0",
                "httpx>=0.20.0",
                "anyio",
                "pyjwt",
                "opentelemetry-api==1.42.1",
                "opentelemetry-sdk==1.42.1",
                "opentelemetry-exporter-otlp-proto-http==1.42.1",
                "opentelemetry-instrumentation==0.63b1",
                "opentelemetry-semantic-conventions==0.63b1",
                "opentelemetry-util-genai==0.3b0",
                "opentelemetry-exporter-gcp-logging==1.12.0a0",
                "opentelemetry-exporter-gcp-trace==1.12.0",
                "opentelemetry-resourcedetector-gcp==1.12.0a0",
                "opentelemetry-instrumentation-google-genai==0.7b1"
            ],
            display_name=f"{prefix}-warehouse-assistant-adk",
            gcs_dir_name=f"{prefix}-reasoning-engine",
            extra_packages=[],
            env_vars={
                "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY": "true",
                "OTEL_SEMCONV_STABILITY_OPT_IN": "gen_ai_latest_experimental",
                "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "EVENT_ONLY",
                "MCP_SERVER_URL": mcp_url
            }
        )
        print("\n====================================================")
        print("ADK Agent Engine Deployed Successfully!")
        print(f"Resource Name: {reasoning_engine.resource_name}")
        print("====================================================")
        return reasoning_engine.resource_name
    except Exception as e:
        print(f"Deployment failed: {e}", file=sys.stderr)
        sys.exit(1)

def main():
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    mcp_url = os.environ.get("MCP_SERVER_URL")
    staging_bucket = os.environ.get("STAGING_BUCKET")
    
    if len(sys.argv) > 1 and sys.argv[1] == "--deploy":
        if not project:
            print("Error: GOOGLE_CLOUD_PROJECT is required for deployment.")
            sys.exit(1)
        if not mcp_url:
            print("Error: MCP_SERVER_URL is required for deployment.")
            sys.exit(1)
        if not staging_bucket:
            staging_bucket = f"gs://{project}-staging"
            print(f"Using default staging bucket: {staging_bucket}")
            
        deploy_agent(project, "us-central1", mcp_url, staging_bucket)
    else:
        print("Running local verification of the ADK Agent (AdkApp)...")
        if not project:
            os.environ["GOOGLE_CLOUD_PROJECT"] = "geap-workshop-temp-1"
        if not mcp_url:
            print("Error: MCP_SERVER_URL is required for local verification.")
            sys.exit(1)
            
        app = create_adk_app()
        app.set_up()
        
        test_query = "List all items in the warehouse inventory"
        print(f"Querying local ADK agent: '{test_query}'...")
        events = list(app.stream_query(user_id="workshop-user", message=test_query))
        print(f"\nReceived {len(events)} events from ADK Agent stream:")
        for i, event in enumerate(events):
            print(f"\n[Event #{i+1}] {event}")

if __name__ == "__main__":
    main()
