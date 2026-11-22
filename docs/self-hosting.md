# Self-hosting Lore

Everything an install needs beyond [README → Install](../README.md#install): running on a server behind your own domain with HTTPS, serving the agent's HTML pages, the bundled LLM router, updates, backups and troubleshooting. Install Lore first, exactly as the README describes; the sections below build on that install.

## Settings to make before the first start

Put these in `.env` next to `docker-compose.yml` **before** the first `docker compose up -d`. Some of them take effect only on the first start.

| Variable | Value | Why |
|---|---|---|
| `LORE_ADMIN_PASSWORD` | a strong password | The first administrator is created once, on the first start. Otherwise sign in and change `lore-admin` right away. |
| `LORE_PORT` | `127.0.0.1:5673` | Lore listens only on the server itself, so the plain-HTTP port is reachable only through your proxy. Leave it as a bare port only when the proxy runs on another host. |

`CORS_ORIGINS` is not needed: Lore accepts writes from a page served at the address the browser used, as long as the proxy passes the original `Host` header (see below). Set it only when a web page served from another origin must write to Lore, or when you serve [HTML pages](#html-pages).

## The reverse proxy

Lore serves plain HTTP. Put a reverse proxy in front of it that terminates TLS for your domain. Whatever proxy you use, it must:

1. **Pass WebSocket upgrades.** Editing, live updates, presence and the AI chat run over WebSocket connections on `/ws/`. Without them no project opens.
2. **Pass the original `Host` header.** Lore refuses a write whose `Origin` does not match the host the request was sent to. If the proxy rewrites `Host` (to `localhost`, to an upstream name), every save fails with 403.
3. **Accept request bodies up to 600 MB.** Audio recordings and files are uploaded in one request.
4. **Keep idle connections open.** A WebSocket stays open for as long as the tab is open; imports and AI requests can take several minutes.
5. **Pass `X-Forwarded-Proto`.** Lore marks the session cookie Secure when the browser came over HTTPS, and learns that from this header. Caddy and Traefik send it by default.

### nginx

```nginx
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    listen 443 ssl;
    server_name lore.example.com;
    # ssl_certificate / ssl_certificate_key: your certificate

    client_max_body_size 600M;

    location / {
        proxy_pass http://127.0.0.1:5673;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
```

The `map` block belongs in the `http` context; a file in `conf.d/` is already inside it. nginx never forwards `Upgrade` and `Connection` on its own, and without `proxy_http_version 1.1` it talks HTTP/1.0 to the upstream, where upgrades do not exist — these three lines are the usual cause of a broken install.

### Other proxies

- **Caddy**, **Traefik**: WebSocket upgrades and the `Host` header pass by default, and neither limits the body size by default. A plain `reverse_proxy 127.0.0.1:5673` (Caddy) or a router to the service (Traefik) is enough.
- **Kubernetes ingress-nginx**: WebSocket works when `/ws/` reaches the same service as `/`. Add the annotations `nginx.ingress.kubernetes.io/proxy-body-size: "600m"`, `proxy-read-timeout: "3600"` and `proxy-send-timeout: "3600"`, and do not use `rewrite-target`.

### Check the proxy

From any machine, with your domain:

```sh
curl -i -N --http1.1 \
  -H "Connection: Upgrade" -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Version: 13" -H "Sec-WebSocket-Key: $(openssl rand -base64 16)" \
  https://lore.example.com/ws/project/00000000-0000-0000-0000-000000000000
```

Expected: `HTTP/1.1 403`. The upgrade reached Lore, and Lore refused it only because curl is not signed in. `404` with `{"detail":"Not Found"}` means the proxy dropped the upgrade headers. Keep `--http1.1`: over HTTP/2 the upgrade headers mean nothing, and the check lies.

## A password in front of Lore

HTTP Basic authentication on the proxy (`auth_basic` in nginx) works with Lore in the browser: after the password prompt the browser sends the credentials with every request, WebSocket included. Lore's own sign-in still applies behind it.

Clients that are not a browser do not know that password. Turn it off for their paths; each of them checks its own key:

```nginx
location /mcp {
    auth_basic off;
    # the same proxy_* lines as in location /
}
location /api/widget/ {
    auth_basic off;
    # the same proxy_* lines as in location /
}
```

- `/mcp` — AI agents and tools connected over MCP (they authenticate with a Lore agent key).
- `/api/widget/` — the upload widget and scripts that use a document upload key.

If one browser (usually Safari) cannot open projects behind the password while others can, turn it off for `/ws/` as well. Every WebSocket requires a Lore session and project access on its own.

## HTML pages

The agent can build web pages — dashboards, calculators, interactive charts — and keep them in the tree as documents. A page runs in a frame loaded from a **second domain**, so its scripts cannot reach the Lore session. Until that domain is set, the agent refuses to create pages and an existing page says that page hosting is not configured; everything else works.

1. **The domain.** It must be a separate registrable domain, not a subdomain of Lore's: `lore-pages.example.net` next to `lore.example.com`, never `pages.lore.example.com` or `pages.example.com`. Lore refuses to start when the two share a domain, because a page there could set cookies for Lore.
2. **The proxy.** Route the new domain to the same Lore port, with TLS and the same rules as the main one (original `Host` header, `X-Forwarded-Proto`). Leave Basic authentication off for it: every page is fetched with its own short-lived signed link.
3. **The settings.** Add to `.env`, with your two addresses, and run `docker compose up -d`:

   ```sh
   ARTIFACT_ORIGIN=https://lore-pages.example.net
   CORS_ORIGINS=https://lore.example.com
   ```

   `CORS_ORIGINS` lists the addresses Lore itself is opened at, comma-separated; it becomes required here, because it also names who may frame the pages. Write `ARTIFACT_ORIGIN` without a port: the bundled web server forwards the host name without it.

The administrator can change the page size limit (5 MB by default) in the admin panel.

## Bundled LLM router

Lore talks to any OpenAI-compatible API. If you do not have one, the install can run ours next to Lore: [llm-api-gateway](https://github.com/kreolsky/llm-api-gateway), one endpoint in front of Anthropic, DeepSeek, OpenRouter, Kimi, z.ai and any other OpenAI-compatible provider, with its own keys and a usage page. It is off until you switch it on, and it is configured by YAML files on the host.

1. Fetch the example config next to `docker-compose.yml`:

   ```sh
   mkdir -p router/config
   for f in providers.yaml models.yaml model_info.yaml user_keys.yaml; do
     curl -fsSL -o "router/config/$f" "https://raw.githubusercontent.com/kreolsky/lore-knowledge-creator/main/router/config.example/$f"
   done
   curl -fsSL -o router/.env https://raw.githubusercontent.com/kreolsky/lore-knowledge-creator/main/router/.env.example
   ```

2. Providers — in `router/config/providers.yaml` delete every provider you have no API key for. Then in `router/config/models.yaml` and `router/config/model_info.yaml` delete every model whose `provider:` is no longer in `providers.yaml`. Put the key of each provider you kept into `router/.env` (`DEEPSEEK_API_KEY=sk-...`) and delete the lines of the others. An empty value (`ANTHROPIC_API_KEY=`) counts as no key: the router refuses to start and its log names the provider.

3. The key Lore uses — mint one (this works before step 4):

   ```sh
   docker compose run --rm --no-deps router python scripts/generate_key.py
   ```

   It prints one line starting with `nnp-v1-`. Replace `REPLACE-with-a-generated-key` in `router/config/user_keys.yaml` with that whole line.

4. Switch it on — add to Lore's `.env`, `AI_API_KEY` being the same line as in step 3:

   ```sh
   COMPOSE_PROFILES=router
   AI_API_URL=http://router:8000/v1
   AI_API_KEY=<the line step 3 printed>
   ```

5. Pick the model — the name Lore asks for is a key under `models:` in `router/config/models.yaml` (`deepseek/flash`), never its `provider_model_name` (`deepseek-v4-flash`). Add it to the same `.env`:

   ```sh
   CHAT_MODEL=deepseek/flash
   ```

   Then `docker compose up -d`. If **Admin panel → Models & APIs** already holds an API URL, key or chat model, it wins over `.env`: set it to the same values there. The example config serves chat models only; speech-to-text and embeddings need their own endpoint or models you add to the router.

6. Check — run all three, in the directory with `docker-compose.yml`:

   ```sh
   docker compose ps router                # STATUS shows (healthy)
   docker compose logs router --tail 30    # no ERROR lines
   docker compose exec backend python -c "import os, urllib.request as u; print(u.urlopen(u.Request('http://router:8000/v1/models', headers={'Authorization': 'Bearer ' + os.environ['AI_API_KEY']})).status)"   # prints 200
   ```

| Symptom | Cause |
|---|---|
| `up` stops at `bind source path does not exist` | `router/config/` missing — step 1 |
| router restarts, log names providers | a provider left in `providers.yaml` has no key in `router/.env` — step 2 |
| the check prints `HTTP Error 401` | `AI_API_KEY` in `.env` differs from `api_key` in `user_keys.yaml` — step 3 |
| the agent answers *No chat model is set* | step 5 skipped, or the admin panel holds an empty value |
| one model fails, others work | its `provider:` was deleted from `providers.yaml` — step 2 |

The router re-reads its YAML every 5 seconds, so later edits to `router/config/` need no restart; edits to `router/.env` need `docker compose up -d router`. The file format is in the router's [README → Configuration](https://github.com/kreolsky/llm-api-gateway#configuration). The router has no port on the host; Lore reaches it inside the install.

The router version is pinned in `docker-compose.yml` and moves with Lore releases; to pin it yourself, set `ROUTER_VERSION=X.Y.Z` in `.env`. A Lore release that moves it updates the examples in `router/config.example/` too — compare them with your copy when the release note says the config format changed.

## Updates and rollback

Update as [README → Update](../README.md#update) describes. Take a backup first ([Backups and restore](#backups-and-restore)): database migrations run automatically when the new version starts, and they run forward only.

To choose each update yourself, pin a release with `LORE_VERSION=vX.Y.Z` in `.env`. What changed in each release, and what an update needs from you, is in [releases/](releases/).

To roll back, set `LORE_VERSION` to the previous release and run `docker compose pull --ignore-buildable` and `docker compose up -d` (the flag skips the agent and sandbox images, which are built from source, not pulled). This is safe only when no database migration lies between the two versions — an older version may refuse to start on a newer database. The release notes say when a release migrates the database; to go back across one, restore the backup taken before the update.

## Backups and restore

Lore keeps its data in Docker volumes. `docker compose down` keeps them; `docker compose down -v` **deletes everything**.

What a backup must hold:

- `lore_surreal` — the database: documents, chats, users, settings;
- `lore_storage` — uploaded files and the key that signs sessions;
- `lore_harness-sessions` — the agent's conversation logs, which a chat continues from;
- `lore_harness-attachments` — the images the agent was shown in chats;
- `lore_sandbox-workspace` — the agent's working files in its sandbox.

The rest is rebuilt on every start and needs no backup: `lore_secrets` (the database password and the agent secret; a fresh one is applied to a restored database), `lore_sandbox-keys` and `lore_redis` (the job queue). `lore_router-usage` holds the bundled router's usage history — add it if you want that kept.

### Back up

Stop the stack, archive the volumes, start it again:

```sh
docker compose stop
docker run --rm \
  -v lore_surreal:/v/surreal -v lore_storage:/v/storage \
  -v lore_harness-sessions:/v/harness-sessions -v lore_harness-attachments:/v/harness-attachments \
  -v lore_sandbox-workspace:/v/sandbox-workspace \
  -v "$PWD":/backup alpine tar czf /backup/lore-backup.tar.gz -C /v .
docker compose start
```

### Restore

On a new machine, install Lore first as the README describes, with the same `LORE_VERSION` the backup was taken on, and copy `lore-backup.tar.gz` next to `docker-compose.yml`. Then empty the volumes and unpack the archive into them:

```sh
docker compose stop
docker run --rm \
  -v lore_surreal:/v/surreal -v lore_storage:/v/storage \
  -v lore_harness-sessions:/v/harness-sessions -v lore_harness-attachments:/v/harness-attachments \
  -v lore_sandbox-workspace:/v/sandbox-workspace \
  -v "$PWD":/backup alpine sh -c 'find /v -mindepth 2 -delete && tar xzf /backup/lore-backup.tar.gz -C /v'
docker compose up -d
```

Check: [README → Check that it runs](../README.md#2-check-that-it-runs), then sign in with an account from the backup. A restore replaces everything written since the backup.

## Troubleshooting

### Install, update and the agent

| Symptom | Check | Fix |
|---|---|---|
| `docker compose up` fails with `address already in use` or `port is already allocated` | another service uses 5673 | put `LORE_PORT=5674` (any free port) in `.env`, run `docker compose up -d`, use that port |
| `env_file` / `required` or `subpath is not allowed` error on `up` | `docker compose version` | upgrade Docker Compose to v2.26.0 or newer (and Docker Engine to 26.0 or newer) |
| after an update the backend log says `the database password file /secrets/surreal/pass is missing or empty` | `docker compose config --services` lists no `secrets-init` | the compose file predates v0.21.0: download it again and run `docker compose up -d --build` ([README → Update](../README.md#update)) |
| health never returns `"status":"ok"` | `docker compose logs backend` | the log names the failing setting or service |
| the backend refuses to start: `ARTIFACT_ORIGIN host … equals or is a subdomain` or `ARTIFACT_ORIGIN is set but CORS_ORIGINS is not` | `.env` | [HTML pages](#html-pages): a separate registrable domain, and `CORS_ORIGINS` set |
| the agent answers *harness service is not reachable*, or health shows `"harness":"unreachable"` | `docker compose logs harness` | the agent is still the previous build: run `docker compose up -d --build` (the agent is built from source, so an update without `--build` keeps the old one) |
| the agent answers *No chat model is set* or *No AI API URL is set* | **Admin panel → Models & APIs** | set the missing value; the next message uses it |
| chat sessions keep untitled-looking names | `docker compose logs harness` | if the log says the title model *advertises reasoning effort levels*, set **Session title model** in **Admin panel → Models & APIs** to a non-reasoning model your endpoint serves |
| forgot the admin password | — | put `LORE_ADMIN_USERNAME=admin2` and `LORE_ADMIN_PASSWORD=<new password>` in `.env`, run `docker compose up -d`; sign in as `admin2@lore.app` and set a new password for the old admin in **Admin panel** |

### Behind a proxy

| Symptom | Cause | Fix |
|---|---|---|
| A project does not open; the browser console shows `WebSocket connection to 'wss://…/ws/…' failed: … Unexpected response code: 404` | The proxy does not pass the WebSocket upgrade | Add `proxy_http_version 1.1` and the `Upgrade` / `Connection` headers (see nginx above), then run the proxy check |
| The same error with `401` | The proxy's Basic password did not reach the WebSocket | Turn `auth_basic` off for `/ws/` |
| Pages load, every save fails with 403 `Origin not allowed` | The proxy replaces the `Host` header | `proxy_set_header Host $host`, or list your public origin in `CORS_ORIGINS` |
| Sign-in works but the session cookie is not marked Secure over HTTPS | The proxy does not send `X-Forwarded-Proto` | `proxy_set_header X-Forwarded-Proto $scheme` (see nginx above) |
| Uploading a large file fails with 413 | The proxy's body size limit | `client_max_body_size 600M` |
| Long imports or uploads fail with 504 | The proxy's read timeout (nginx default 60s) | `proxy_read_timeout 3600s` |
| MCP clients or upload scripts get 401 with an HTML page | The proxy's Basic password | `auth_basic off` for `/mcp` and `/api/widget/` |
