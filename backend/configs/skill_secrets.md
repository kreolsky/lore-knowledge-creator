---
name: secrets
description: Use when the task needs one of the user's personal API keys saved in the cabinet. «вызови API с моим ключом», «запроси через мой токен», «личный ключ»; use my key, call the API with my key.
tools:
  - list_secrets
  - http_request
---

# Personal keys

The user keeps personal API keys in Lore's cabinet (settings). You see each key's
name, description and bound hosts — never its value; `http_request` injects it
server-side. Asking the user to paste a key into the chat is always wrong.

## Flow

1. `list_secrets` — the enabled keys.
2. `http_request` with `secret` (the name) and a `url` on one of that row's hosts.

GET and HEAD run without an ask. Every other method is held for approval on EACH call,
even in auto mode — plan writes as discrete, named calls and expect the ask. Data goes
in a body, so a call that sends data is a POST.

A 3xx is returned, not followed: read its `Location` and decide; never assume it was
applied. A large or binary response → `save: true`.

## When the call fails

- 404 naming the secret: no enabled key by that name — re-run `list_secrets`; the
  user may have renamed or disabled it.
- 409 asking to re-enter the key: the stored value can no longer be decrypted — tell
  the user to re-enter it in the cabinet.
- 403 naming the host binding: not retryable with other arguments — tell the user to
  rebind the key to that host in the cabinet.
