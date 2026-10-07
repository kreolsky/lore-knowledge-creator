# The agent line — the RENTED loop (DeepSeek Harness) behind Lore's driver
# contract. The image pins the harness at the tag dsh-v0.2.0-rc.2
# (= 639ed015, 2026-09-29 — the newest rc; alphas and master are never
# pinned). The pin moves ATOMICALLY with vendor/dsh and the browser bundle
# banners (dsh-pin-gate.py); the harness migrates its OWN session logs on
# open (format v0→v4), so logs written by older images keep loading.
FROM node:24-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates python3 make g++ \
    && rm -rf /var/lib/apt/lists/*
RUN corepack enable

WORKDIR /dsh
ARG DSH_SHA=639ed015397290b3745d163aafe02ffee4aa3f84
RUN git clone --filter=blob:none https://github.com/deepseek-ai/deepseek-harness.git . \
    && git checkout $DSH_SHA
RUN pnpm install --frozen-lockfile && pnpm run build

# The Lore plugin lives INSIDE the harness workspace so its imports resolve
# against the workspace packages (same shape as the spike's spike-plugin).
COPY plugin /dsh/lore-driver
# The DSH_HOME composition: the lore profile (dsh-base only) + the home patch
# carrying the plugin insert, the llm-pi-ai `lore` LLM route (plugin-upserted
# catalog) and the zero-tool disables.
COPY home /app/home

# The headless assembler bundle the BROWSER runs (see SYSTEM: dsh-conversation).
# Built here because this image already builds the whole dsh workspace, so the
# bundle and the loop are the same sha by construction. It is COPIED BEFORE the
# plugin tests because the plugin imports the lore event registry from it
# (map.ts -> ../../lore-conversation/src/lore-events.ts); the bundle itself is
# extracted to the frontend by conversation/sync-bundle.sh.
COPY conversation /dsh/lore-conversation

# Build-time plugin tests (pure mapping/projection). entries.test.ts gates the reload replay (the live
# frames and the replayed ones come off the same mapEvent); ws-events.test.ts gates the standing
# event channel (the /ws/events protocol: same frames, per-session subscribe, auth);
# approvals.test.ts gates the dsh approval bridge (the parked ask, grants,
# the 409/404 expiry split); skills.test.ts gates the lore-skills provider +
# the served-toolset filter;  images.test.ts gates the multimodal prompt blocks + the per-turn
# image-capability upsert; caps.test.ts gates the model-capability resolution
# (the ONE source); web-search.test.ts gates the Lore search providers' wire
# mapping + call-time credential resolution; boot-env.test.ts gates the
# standalone boot (no backend fetch rides the entry); time-stamps.test.ts gates
# the turn's time ground as its own lore-time context message.
RUN node --import tsx/esm --test lore-driver/test/map.test.ts lore-driver/test/entries.test.ts lore-driver/test/driver.test.ts lore-driver/test/ws-events.test.ts lore-driver/test/approvals.test.ts lore-driver/test/skills.test.ts lore-driver/test/images.test.ts lore-driver/test/caps.test.ts lore-driver/test/web-search.test.ts lore-driver/test/boot-env.test.ts lore-driver/test/time-stamps.test.ts

# The test feeds a real session log.
RUN node --import tsx/esm --test lore-conversation/test/conversation.test.ts lore-conversation/test/lore-nodes.test.ts
RUN cd /dsh/lore-conversation && pnpm exec tsdown --config tsdown.config.ts \
    && node test/bundle-smoke.mjs

ENV DSH_HOME=/app/home
ENV PORT=8090
EXPOSE 8090

# boot-env.ts fetches the web-search env from the backend, then runs the CLI
# in-process on the lore profile; the plugin's HTTP server keeps the process
# alive (no headless/web app bundle — nothing exits on its own).
CMD ["node", "--import", "tsx/esm", "lore-driver/src/boot-env.ts", "--profile", "lore"]
