"""AI Chat routes — sessions CRUD, message tree, LLM completions (driver-owned turns), transcription."""
# SYSTEM: chat-routes — AI chat sessions, message tree, LLM completions (driver-owned), context assembly
# ARCH: the API key never reaches the client — the driver speaks to the gateway.
# ARCH: every turn is DRIVER-owned —
# POST /completions answers JSON and the frames ride the project WS
# (SYSTEM: chat-fanout) + the reload path.
# ARCH: Message tree via parent_id. Fork = sibling message with same parent_id.

# WHY agent.keys in this block: it registers its module-level access_changed
# subscriber (standing-key revocation) at import — it must be live from
# process start, under BOTH the real lifespan and the ASGITransport test
# client (which skips lifespans).
import agent.keys  # noqa: F401

import routes.chat.completions  # noqa: F401
import routes.chat.context  # noqa: F401
import routes.chat.messages  # noqa: F401
import routes.chat.models_catalog  # noqa: F401 — binds GET /models (gateway catalog moved out of completions)
import routes.chat.serializers  # noqa: F401
import routes.chat.sessions  # noqa: F401
import routes.chat.sessions_list  # noqa: F401 — binds GET /sessions (list path moved out of sessions)
import routes.chat.transcription  # noqa: F401
import routes.chat.verdicts  # noqa: F401 — binds GET/POST /api/chat/verdicts
from routes.chat._router import router  # noqa: F401

__all__ = ["router"]
