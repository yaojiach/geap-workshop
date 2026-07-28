import os
import re
import sys
import json
import time
import urllib.request
import urllib.error
import google.auth
import google.auth.transport.requests

# Deleting a registry service returns a long-running operation, so the entry stays visible
# for a few seconds afterwards. Re-creating it too soon fails with HTTP 409 ALREADY_EXISTS.
DELETE_POLL_TIMEOUT_SECONDS = 90

def get_auth_token():
    credentials, _ = google.auth.default()
    auth_req = google.auth.transport.requests.Request()
    credentials.refresh(auth_req)
    return credentials.token

def wait_until_deleted(service_url: str, token: str):
    """Polls the service until it returns 404, so the follow-up create does not hit a 409."""
    deadline = time.time() + DELETE_POLL_TIMEOUT_SECONDS
    while time.time() < deadline:
        req = urllib.request.Request(service_url, method="GET")
        req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req):
                pass
        except urllib.error.HTTPError as e:
            if e.code == 404:
                print("Existing registry entry is fully deleted.")
                return
        except Exception:
            return
        print(".", end="", flush=True)
        time.sleep(3)
    print(f"\nWarning: registry entry still present after {DELETE_POLL_TIMEOUT_SECONDS}s; "
          "continuing anyway.")

def register_mcp_server(project_id: str, location: str, mcp_url: str):
    prefix = os.environ.get("GEAP_PREFIX")
    if not prefix:
        print("Error: GEAP_PREFIX environment variable is not set.")
        sys.exit(1)
    
    token = get_auth_token()
    service_id = f"{prefix}-warehouse-db"
    
    # 1. First try to delete the service if it already exists to start clean
    delete_url = f"https://agentregistry.googleapis.com/v1alpha/projects/{project_id}/locations/{location}/services/{service_id}"
    print(f"Checking for existing registration at: {delete_url}")
    req = urllib.request.Request(delete_url, method="DELETE")
    req.add_header("Authorization", f"Bearer {token}")
    
    try:
        with urllib.request.urlopen(req) as response:
            print("Deletion requested; waiting for it to finish...", end="", flush=True)
            wait_until_deleted(delete_url, token)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print("No existing registry entry found.")
        else:
            print(f"Warning: Deletion returned HTTP status {e.code}: {e.read().decode()}")
    except Exception as e:
        print(f"Warning: Failed to delete existing registry: {e}")

    # 2. Register/Create the service
    create_url = f"https://agentregistry.googleapis.com/v1alpha/projects/{project_id}/locations/{location}/services?serviceId={service_id}"
    print(f"\nRegistering MCP server in Agent Registry at: {create_url}")
    
    payload = {
        "displayName": "Warehouse Database Toolset",
        "description": "Exposes tools to check inventory levels, update stock, and process customer orders in the postgres DB.",
        "mcpServerSpec": {
            "type": "TOOL_SPEC",
            "content": {
                "tools": [
                    {
                        "name": "list_inventory",
                        "description": "Lists all products currently in stock in the warehouse inventory, showing product ID, name, description, quantity, and price.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {}
                        }
                    },
                    {
                        "name": "get_product_stock",
                        "description": "Gets the stock level and details for a specific product ID.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "product_id": {
                                    "type": "integer"
                                }
                            },
                            "required": ["product_id"]
                        }
                    },
                    {
                        "name": "update_stock",
                        "description": "Updates the stock level of a specific product ID to a new quantity.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "product_id": {
                                    "type": "integer"
                                },
                                "quantity": {
                                    "type": "integer"
                                }
                            },
                            "required": ["product_id", "quantity"]
                        }
                    },
                    {
                        "name": "create_order",
                        "description": "Creates a customer order for a product and automatically decrements the warehouse stock if inventory is available.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "product_id": {
                                    "type": "integer"
                                },
                                "quantity": {
                                    "type": "integer"
                                },
                                "customer_name": {
                                    "type": "string"
                                }
                            },
                            "required": ["product_id", "quantity", "customer_name"]
                        }
                    }
                ]
            }
        },
        "interfaces": [
            {
                "url": mcp_url,
                "protocolBinding": "JSONRPC"
            }
        ]
    }

    for attempt in range(1, 4):
        req = urllib.request.Request(
            create_url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST"
        )
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(req) as response:
                resp_body = response.read().decode()
                print("Successfully registered service in Agent Registry!")
                print(json.dumps(json.loads(resp_body), indent=2))
                return
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            if e.code == 409 and attempt < 3:
                print(f"Registry still reports the service as existing (attempt {attempt}/3); "
                      "retrying in 10s...")
                time.sleep(10)
                continue
            print(f"\nError registering service (HTTP {e.code}): {e.reason}")
            print(body)
            sys.exit(1)
        except Exception as e:
            print(f"\nFailed to register service: {e}")
            sys.exit(1)

def main():
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    mcp_url = os.environ.get("MCP_SERVER_URL")

    if not project:
        print("Error: GOOGLE_CLOUD_PROJECT environment variable is not set.")
        sys.exit(1)

    if not mcp_url:
        print("Error: MCP_SERVER_URL environment variable is not set.")
        sys.exit(1)

    # First try global, if it fails or isn't appropriate, we can customize
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")

    register_mcp_server(project, location, mcp_url)

if __name__ == "__main__":
    main()
