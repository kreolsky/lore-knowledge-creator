---
name: secrets
description: Use when the task needs one of the user's personal API keys — calling an external service with the billing key, the company API, a personal token the user saved in the cabinet. «Вызови API с моим ключом», «проверь на биллинге», «запроси через мой токен», use my key, call the API with my key, check the billing key, personal token, личный ключ. Activates list_secrets and http_request.
tools:
  - list_secrets
  - http_request
---

# Personal keys

The user keeps personal API keys in Lore's cabinet (settings). You see each key's NAME,
description and the hosts it is bound to — never its value. `http_request` injects the
value server-side and scrubs it from everything you read back; asking the user to paste
a key into the chat is always wrong.

## Flow

1. `list_secrets` — the enabled keys with their `{name, description, hosts}`.
2. `http_request` with `secret` (the name), `url` (https, host must be one of the row's
   bound hosts), `method`, optional `headers` and `body`.

GET and HEAD run without a confirmation. Every other method is held for the user's
approval on EACH call — even when the user's apply preference is auto, and "allow for
the session" is not offered — so plan writes
as discrete, named calls, and expect the ask.

## Hard bounds (server-enforced, don't fight them)

- The URL's host must be one of the secret's bound hosts (port included when not 443).
  A key saved for `api.example.com` cannot be spent anywhere else.
- https only. No keys in a URL query. No redirects followed — a 3xx comes back as the
  result; read its `Location` and decide, don't assume it was applied.
- You cannot set `Host`, `Authorization`, the secret's own header, `Accept-Encoding` or
  a method override — the server owns those.
- GET and HEAD carry no body; a call that sends data is a POST, and gets the ask.
- The response body returns inline up to a cap (`truncated` tells you); larger or
  binary responses go with `save: true`, which writes the response into your sandbox
  workspace and returns the path.

## When the key is the wrong answer

A 404 naming the secret means no enabled row by that name — re-run `list_secrets`, the
user may have renamed or disabled it. A 409 asking to re-enter the key means the stored
value can no longer be decrypted; tell the user to re-enter it in the cabinet. A 403
naming the host binding is not retryable with arguments — the user must rebind the key
in the cabinet, and you should say exactly that.
