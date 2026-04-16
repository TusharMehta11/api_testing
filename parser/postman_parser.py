"""
Parse a Postman Collection v2.1 JSON file into a flat list of endpoint dicts.
Each dict contains: name, method, url, headers, body, auth, folder_path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _resolve_url(url_field: Any) -> str:
    """Return a plain URL string from Postman's url field (string or object)."""
    if isinstance(url_field, str):
        return url_field
    if isinstance(url_field, dict):
        raw = url_field.get("raw", "")
        if raw:
            return raw
        # Reconstruct from parts
        protocol = url_field.get("protocol", "http")
        host = ".".join(url_field.get("host", []))
        path_parts = "/".join(url_field.get("path", []))
        port = url_field.get("port", "")
        base = f"{protocol}://{host}"
        if port:
            base += f":{port}"
        if path_parts:
            base += f"/{path_parts}"
        query = url_field.get("query", [])
        if query:
            qs = "&".join(
                f"{q.get('key', '')}={q.get('value', '')}"
                for q in query
                if not q.get("disabled", False)
            )
            base += f"?{qs}"
        return base
    return ""


def _parse_headers(header_list: list[dict]) -> dict[str, str]:
    """Convert Postman header array to a plain dict, skipping disabled entries."""
    return {
        h["key"]: h.get("value", "")
        for h in header_list
        if isinstance(h, dict) and not h.get("disabled", False) and "key" in h
    }


def _parse_body(body: dict | None) -> str:
    """Extract body content as a string from Postman body object."""
    if not body:
        return ""
    mode = body.get("mode", "")
    if mode == "raw":
        return body.get("raw", "")
    if mode == "urlencoded":
        pairs = body.get("urlencoded", [])
        return "&".join(
            f"{p.get('key', '')}={p.get('value', '')}"
            for p in pairs
            if not p.get("disabled", False)
        )
    if mode == "formdata":
        pairs = body.get("formdata", [])
        return "&".join(
            f"{p.get('key', '')}={p.get('value', '')}"
            for p in pairs
            if not p.get("disabled", False)
        )
    if mode == "graphql":
        gql = body.get("graphql", {})
        return json.dumps(gql)
    return ""


def _extract_auth(auth_obj: dict | None) -> dict:
    """Return a simple auth dict: {type, username, password, token}."""
    if not auth_obj:
        return {"type": "noauth"}
    auth_type = auth_obj.get("type", "noauth")
    result: dict[str, str] = {"type": auth_type}

    def _kv_list(key: str) -> dict:
        """Postman stores auth params as [{key, value}] lists."""
        items = auth_obj.get(key, [])
        return {i["key"]: i.get("value", "") for i in items if "key" in i}

    if auth_type == "basic":
        params = _kv_list("basic")
        result["username"] = params.get("username", "")
        result["password"] = params.get("password", "")
    elif auth_type == "bearer":
        params = _kv_list("bearer")
        result["token"] = params.get("token", "")
    elif auth_type == "apikey":
        params = _kv_list("apikey")
        result["key"] = params.get("key", "")
        result["value"] = params.get("value", "")
        result["in"] = params.get("in", "header")

    return result


def _walk_items(
    items: list[dict],
    folder_path: str,
    collection_auth: dict | None,
) -> list[dict]:
    """
    Recursively walk Postman items.
    Items can be folders (have nested 'item') or requests.
    """
    endpoints: list[dict] = []

    for item in items:
        if "item" in item:
            # This is a folder — recurse
            folder_name = item.get("name", "")
            child_path = f"{folder_path}/{folder_name}" if folder_path else folder_name
            children = _walk_items(item["item"], child_path, collection_auth)
            endpoints.extend(children)
        else:
            # This is a request
            request = item.get("request", {})
            if not request:
                continue

            method = request.get("method", "GET").upper()
            url = _resolve_url(request.get("url", ""))
            headers = _parse_headers(request.get("header", []))
            body_str = _parse_body(request.get("body"))
            auth = _extract_auth(request.get("auth") or collection_auth)
            description = request.get("description", "")
            if isinstance(description, dict):
                description = description.get("content", "")

            endpoints.append(
                {
                    "name": item.get("name", url),
                    "method": method,
                    "url": url,
                    "headers": headers,
                    "body": body_str,
                    "auth": auth,
                    "description": description,
                    "folder_path": folder_path,
                }
            )

    return endpoints


def parse_collection(file_path: str | Path) -> list[dict]:
    """
    Parse a Postman Collection v2.0 / v2.1 JSON file.

    Returns a flat list of endpoint dicts:
        {
            name: str,
            method: str,
            url: str,
            headers: dict,
            body: str,
            auth: dict,
            description: str,
            folder_path: str,
        }
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Collection file not found: {path}")

    with path.open(encoding="utf-8") as f:
        data = json.load(f)

    # Support both top-level collection wrapper and bare collection object
    collection = data.get("collection", data)
    info = collection.get("info", {})
    schema = info.get("schema", "")
    if schema and "v2" not in schema.lower() and "2.1" not in schema and "2.0" not in schema:
        raise ValueError(
            f"Unsupported Postman collection schema: {schema}. "
            "Only Collection v2.0 / v2.1 are supported."
        )

    collection_auth = _extract_auth(collection.get("auth")) if collection.get("auth") else None
    items = collection.get("item", [])
    endpoints = _walk_items(items, folder_path="", collection_auth=collection_auth)
    return endpoints
