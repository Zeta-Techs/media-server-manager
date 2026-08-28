from __future__ import annotations

import uuid
from typing import Any, Dict, List
from urllib.parse import urlencode

import requests

from clp.core import DEFAULT_CLIENT_IDENTIFIER, PLEX_PRODUCT, plex_headers

from .db import get_setting, set_setting

PLEX_TV = "https://plex.tv"
PLEX_APP_AUTH = "https://app.plex.tv/auth"


def get_client_identifier() -> str:
    existing = get_setting("plex_client_identifier")
    if existing:
        return existing
    client_id = f"{DEFAULT_CLIENT_IDENTIFIER}-{uuid.uuid4()}"
    set_setting("plex_client_identifier", client_id)
    return client_id


def create_pin(forward_url: str, timeout: int = 20) -> Dict[str, Any]:
    client_id = get_client_identifier()
    response = requests.post(
        f"{PLEX_TV}/api/v2/pins",
        params={"strong": "true"},
        headers=plex_headers(client_identifier=client_id),
        timeout=timeout,
    )
    response.raise_for_status()
    pin = response.json()
    auth_url = build_auth_url(pin["code"], client_id, forward_url)
    return {"pin_id": pin["id"], "code": pin["code"], "auth_url": auth_url}


def build_auth_url(code: str, client_id: str, forward_url: str) -> str:
    params = {
        "clientID": client_id,
        "code": code,
        "forwardUrl": forward_url,
        "context[device][product]": PLEX_PRODUCT,
    }
    return f"{PLEX_APP_AUTH}#?{urlencode(params)}"


def check_pin(pin_id: int | str, timeout: int = 20) -> Dict[str, Any]:
    client_id = get_client_identifier()
    response = requests.get(
        f"{PLEX_TV}/api/v2/pins/{pin_id}",
        headers=plex_headers(client_identifier=client_id),
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()
    token = data.get("authToken")
    result: Dict[str, Any] = {"claimed": bool(token), "pin": data}
    if token:
        result["auth_token"] = token
        result["servers"] = discover_servers(token)
    return result


def discover_servers(token: str, timeout: int = 20) -> List[Dict[str, Any]]:
    client_id = get_client_identifier()
    response = requests.get(
        f"{PLEX_TV}/api/v2/resources",
        params={"includeHttps": "1", "includeRelay": "1"},
        headers=plex_headers(token, client_id),
        timeout=timeout,
    )
    response.raise_for_status()
    resources = response.json()
    servers = []
    for resource in resources:
        provides = resource.get("provides", "")
        if "server" not in provides and not resource.get("server"):
            continue
        connections = normalize_connections(resource.get("connections") or [])
        servers.append(
            {
                "name": resource.get("name") or resource.get("product") or "Plex Server",
                "client_identifier": resource.get("clientIdentifier", ""),
                "owned": bool(resource.get("owned")),
                "connections": connections,
                "best_connection": choose_best_connection(connections),
                "token": token,
            }
        )
    return servers


def normalize_connections(connections: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    normalized = []
    for connection in connections:
        uri = (connection.get("uri") or "").rstrip("/")
        if not uri:
            continue
        normalized.append(
            {
                "uri": uri,
                "local": bool(connection.get("local")),
                "relay": bool(connection.get("relay")),
                "protocol": connection.get("protocol", ""),
                "address": connection.get("address", ""),
                "port": connection.get("port", ""),
            }
        )
    return normalized


def choose_best_connection(connections: List[Dict[str, Any]]) -> Dict[str, Any] | None:
    if not connections:
        return None
    sorted_connections = sorted(
        connections,
        key=lambda item: (
            0 if item.get("local") else 1,
            0 if item.get("protocol") == "https" else 1,
            1 if item.get("relay") else 0,
        ),
    )
    return sorted_connections[0]
