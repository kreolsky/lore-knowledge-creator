"""Personal-secrets agent tool CONSTANTS: list_secrets + http_request.
Pure data — imports nothing (leaf), mirroring specs/skills.py.

# ARCH: the pack is agent-only (surfaces agent + tool_api, never mcp) and
# console-key-gated at the handler (`_require_console_key`, the same predicate
# as sandbox_bash): an external MCP client or a subtree-scoped key must never
# spend the user's personal keys. The value never reaches the model — the
# handler injects the header and scrubs the value (three encoded forms) from
# everything it returns (SYSTEM: user-secrets).
"""

LIST_SECRETS_TOOL: dict = {
    "type": "function",
    "mutating": False,
    "function": {
        "name": "list_secrets",
        "description": (
            "List the user's personal keys you may spend with http_request: "
            "{name, description, hosts} per row. Only `enabled` rows are "
            "listed. The VALUE is never shown to you — it is injected "
            "server-side into the request header; never ask the user to "
            "paste a key into the chat. Rows are managed in the cabinet "
            "(Lore's settings), not by any tool."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
}

HTTP_REQUEST_TOOL: dict = {
    "type": "function",
    "mutating": True,
    "function": {
        "name": "http_request",
        "description": (
            "Call an external HTTPS API with one of the user's personal keys "
            "(`secret`, by name from list_secrets). The key's VALUE never "
            "reaches you: the server injects it into the header the row "
            "names (usually `Authorization: Bearer …`) and scrubs it from "
            "everything you see. The URL's host must be one of the secret's "
            "bound hosts — a key saved for `api.example.com` cannot be sent "
            "anywhere else; a bound host `*.example.com` covers its "
            "subdomains. Some rows carry the key in a URL query parameter "
            "instead — the server adds it; never put a key in the URL "
            "yourself. Plain http is not supported. Redirects are never followed (a 3xx is returned as "
            "the result). GET and HEAD run silently; every other method "
            "needs the user's approval on THIS call even in auto mode — a "
            "foreign write under the user's name has no history to roll "
            "back. The text body returns inline (truncated when long); "
            "`save: true` writes the response to your sandbox workspace "
            "instead and returns the path. Response download is capped at "
            "20 MB."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "secret": {
                    "type": "string",
                    "description": (
                        "The personal key's name, exactly as list_secrets "
                        "shows it."
                    ),
                },
                "url": {
                    "type": "string",
                    "description": (
                        "Full `https://` URL. Its host (and port, when not "
                        "443) must match one of this secret's bound hosts "
                        "(a `*.domain` entry matches its subdomains)."
                    ),
                },
                "method": {
                    "type": "string",
                    "enum": ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"],
                    "default": "GET",
                    "description": (
                        "GET and HEAD run without an ask; every other method "
                        "is held for the user's approval on each call."
                    ),
                },
                "headers": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": (
                        "Extra request headers. `Host`, `Authorization`, "
                        "the secret's own header, `Accept-Encoding` and "
                        "method overrides are stripped — the server controls "
                        "those."
                    ),
                },
                "body": {
                    "type": ["string", "null"],
                    "description": (
                        "Request body as text (JSON, form, XML — up to 1 "
                        "MB). Not accepted on GET and HEAD."
                    ),
                },
                "save": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Write the response body to your sandbox workspace "
                        "(inbox/http/…) and return its path instead of the "
                        "inline text — for bodies over the inline limit or "
                        "not readable as text."
                    ),
                },
                "apply": {
                    "type": "string",
                    "enum": ["confirm", "auto"],
                    "default": "confirm",
                    "description": (
                        "The mid-turn approval axis. NOTE: unlike document "
                        "tools, `auto` does NOT skip the ask — every "
                        "non-GET/HEAD http_request is held for the user's "
                        "approval regardless."
                    ),
                },
            },
            "required": ["secret", "url"],
        },
    },
}
