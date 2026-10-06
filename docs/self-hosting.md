# Running Lore on a server

This guide puts a Lore install on a server behind your own domain with HTTPS. Install Lore on the server first, exactly as [README → Install](../README.md#install) describes. Everything below is what a server adds on top of that install.

## Settings to make before the first start

Put these in `.env` next to `docker-compose.yml` **before** the first `docker compose up -d`. Some of them take effect only on the first start.

| Variable | Value | Why |
|---|---|---|
| `LORE_ADMIN_PASSWORD` | a strong password | The first administrator is created once, on the first start. Otherwise sign in and change `lore-admin` right away. |
| `LORE_PORT` | `127.0.0.1:8080` | Lore listens only on the server itself, so the plain-HTTP port is reachable only through your proxy. Leave it as a bare port only when the proxy runs on another host. |

`CORS_ORIGINS` is not needed: Lore accepts writes from a page served at the address the browser used, as long as the proxy passes the original `Host` header (see below). Set it only when a web page served from another origin must write to Lore.

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
        proxy_pass http://127.0.0.1:8080;
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

- **Caddy**, **Traefik**: WebSocket upgrades and the `Host` header pass by default, and neither limits the body size by default. A plain `reverse_proxy 127.0.0.1:8080` (Caddy) or a router to the service (Traefik) is enough.
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

## Updates and rollback

Update as [README → Update](../README.md#update) describes. Take a backup first ([README → Data and backups](../README.md#data-and-backups)): database migrations run automatically when the new version starts, and they run forward only.

To choose each update yourself, pin a release with `LORE_VERSION=vX.Y.Z` in `.env`. What changed in each release, and what an update needs from you, is in [releases/](releases/).

To roll back, set `LORE_VERSION` to the previous release and run `docker compose pull --ignore-buildable` and `docker compose up -d` (the flag skips the agent and sandbox images, which are built from source, not pulled). This is safe only when no database migration lies between the two versions — an older version may refuse to start on a newer database. The release notes say when a release migrates the database; to go back across one, restore the backup taken before the update.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| A project does not open; the browser console shows `WebSocket connection to 'wss://…/ws/…' failed: … Unexpected response code: 404` | The proxy does not pass the WebSocket upgrade | Add `proxy_http_version 1.1` and the `Upgrade` / `Connection` headers (see nginx above), then run the proxy check |
| The same error with `401` | The proxy's Basic password did not reach the WebSocket | Turn `auth_basic` off for `/ws/` |
| Pages load, every save fails with 403 `Origin not allowed` | The proxy replaces the `Host` header | `proxy_set_header Host $host`, or list your public origin in `CORS_ORIGINS` |
| Sign-in works but the session cookie is not marked Secure over HTTPS | The proxy does not send `X-Forwarded-Proto` | `proxy_set_header X-Forwarded-Proto $scheme` (see nginx above) |
| Uploading a large file fails with 413 | The proxy's body size limit | `client_max_body_size 600M` |
| Long imports or uploads fail with 504 | The proxy's read timeout (nginx default 60s) | `proxy_read_timeout 3600s` |
| MCP clients or upload scripts get 401 with an HTML page | The proxy's Basic password | `auth_basic off` for `/mcp` and `/api/widget/` |
