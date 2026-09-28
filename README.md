# Lore

Collaborative editor for worldbuilding and lore: real-time co-editing, annotations, references, version history, access control, and an AI assistant.

## Quick start

Requirements: Docker with Compose v2, and an OpenAI-compatible API endpoint (for example a LiteLLM proxy) for speech-to-text and the AI assistant.

```sh
git clone --recurse-submodules <this repository> lore
cd lore
cp .env.example .env
```

Edit `.env` and set at least:

- `LORE_SECRET_KEY` — a long random string (`openssl rand -hex 32`);
- `LORE_ADMIN_USERNAME`, `LORE_ADMIN_PASSWORD` — the admin account created on first boot;
- `AI_API_URL`, `AI_API_KEY` — your API endpoint, and `CHAT_MODEL` / `HARNESS_MODEL` set to model ids it serves;
- `HARNESS_DRIVER_SECRET` — a random string, enables the AI assistant.

Every variable is documented in `.env.example`. Then:

```sh
docker compose up -d --build
```

Open http://localhost:5173 and sign in as the admin.

The agent's shell sandbox is optional: generate a key pair, put the public half in `sandbox/authorized_keys` and the private half, base64-encoded, in `SANDBOX_SSH_KEY_B64`. Without it the assistant simply has no shell tool.

## Prebuilt images

Released images are published to `ghcr.io/kreolsky` (`lore-backend`, `lore-converter`, `lore-frontend`), so a server needs no source build. The AI harness image is not published, because it bundles a dependency that may not be redistributed; build it yourself under the same name:

```sh
cp .env.example .env    # fill it in as above, plus:
                        # LORE_REGISTRY=ghcr.io/kreolsky
                        # LORE_IMAGE_TAG=<release tag, e.g. v0.20.0>
docker build -t ghcr.io/kreolsky/lore-harness:<release tag> harness-driver
docker compose -f docker-compose.prod.yml up -d
```

Compose pulls only the images it does not have, so the locally built harness is used. Data lives under `LORE_DATA_ROOT` (default `/opt/lore/data`); create its `surreal`, `storage`, `redis` and `harness-sessions` subdirectories first.

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).

## License

Lore is **source-available** under the [Business Source License 1.1](LICENSE). It is not an OSI-approved open source license. Each version automatically converts to the **Apache License 2.0** four years after it is published.

### Free to use

You can use Lore in production for free, with no feature limits, if you and your affiliates (parent companies and subsidiaries) meet **all** of the following:

- less than **USD 1M gross revenue** in the last 12 months;
- less than **USD 1M total outside funding** raised;
- no more than **25 people** working for you, counting employees and contractors.

Individuals, non-profits, schools and universities, and any non-commercial use are always free, whatever their size. Evaluation, development and testing are free for everyone.

### Needs a commercial license

- Organizations above any of the thresholds above.
- **Anyone, of any size**, offering Lore to third parties as a hosted or managed service.

If your studio grows past a threshold, you have **90 days** to get a commercial license. Contact: zaigraeff@gmail.com.

### Examples

| Situation | License |
|---|---|
| A 6-person indie studio, $300k revenue, self-funded | Free |
| A solo writer building a setting for a novel | Free |
| A 12-person studio that got a $400k advance from a publisher | Free (a publisher advance counts as revenue, not funding) |
| A 10-person startup that raised a $3M seed round | Commercial |
| A studio owned by a large publisher, even if the studio is small | Commercial (affiliates count) |
| A company hosting Lore for its customers | Commercial |

Third-party components and their licenses are listed in [NOTICE](NOTICE). The document converter service in `converter/` is licensed separately under the [GNU AGPL v3.0](converter/LICENSE).
