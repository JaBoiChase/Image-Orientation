import os, json
import requests
from dotenv import load_dotenv

load_dotenv()

SHOP = os.environ["SHOPIFY_SHOP"]
TOKEN = os.environ["SHOPIFY_ADMIN_TOKEN"]
API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2026-01")

GQL_ENDPOINT = f"https://{SHOP}/admin/api/{API_VERSION}/graphql.json"

def gql(query: str, variables: dict | None = None) -> dict:
    resp = requests.post(
        GQL_ENDPOINT,
        headers={
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": TOKEN,
        },
        data=json.dumps({"query": query, "variables": variables or {}}),
        timeout=60,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("errors"):
        raise RuntimeError(payload["errors"])
    return payload["data"]

def product_gid(numeric_id: int) -> str:
    return f"gid://shopify/Product/{numeric_id}"

GET_PRODUCT = """
query getProduct($id: ID!) {
  product(id: $id) {
    id
    title
    vendor
    media(first: 100) {
      nodes {
        __typename
        ... on MediaImage {
          id
          alt
          fileStatus
          image { url }
        }
      }
    }
    variants(first: 100) {
      nodes {
        id
        selectedOptions {
          name
          value
        }
      }
    }
  }
}
"""

FILE_UPDATE = """
mutation fileUpdate($files: [FileUpdateInput!]!) {
  fileUpdate(files: $files) {
    userErrors { code field message }
    files {
      id
      alt
      ... on MediaImage {
        image { url }
      }
    }
  }
}
"""
