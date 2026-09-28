"""Tool-API package — the agent's permission boundary: every tool call passes it.

# SYSTEM: tool-api — the permission boundary every tool call passes; ours by
#   design, independent of the brain.
#   (Entry module: _common.py holds the auth dependency + apply primitives; the
#   handler domains below hold the tool handlers.)

# ARCH (package layout, replaces the former routes/tool_api.py monolith): a shared
# _router.py holds the one APIRouter; _common.py is the leaf spine (auth +
# apply primitives); the per-tool domains reads/edits/creates import _common +
# _router and expose plain handlers. The agent-tool POST routes are GENERATED from
# the agent_tools registry (one declaration per tool) by bind_tool_api_routes
# below. The proposal apply/reject routes were deleted with the proposal cluster
# (mid-turn approval holds the call instead).
# WHY generated routes: one declaration
# per tool — a hand-written route beside the registry is a second copy the
# registry cannot retire.

# Facade-free by decision (plan fewer-layers): the imports below are ROUTE
# REGISTRATION (the handler modules must be imported so the registry resolves
# their bindings) — nothing is re-exported. Consumers import the OWNING module:
# `routes.tool_api._common` (auth + apply primitives), `routes.tool_api.edits`,
# `.creates`, `.reads`, … `main.py` reaches the router via `tool_api.router`.

# Import DAG: _common is a pure leaf; domain modules import ONLY _common +
# models (+ their own leaves); no domain imports another domain. Nothing in
# routes.chat.* imports this package back → no load-time cycle.
"""
from agent_tools.routing import bind_tool_api_routes  # noqa: E402

import routes.tool_api.creates  # noqa: F401
import routes.tool_api.edits  # noqa: F401
import routes.tool_api.image_gen  # noqa: F401
import routes.tool_api.imports  # noqa: F401

# Side-effect imports: the handler definitions the registry resolves. The
# bindings are registered in agent_tools.entries (imported via the agent_tools
# package), independent of module import order.
import routes.tool_api.memory  # noqa: F401
import routes.tool_api.reads  # noqa: F401
import routes.tool_api.sandbox  # noqa: F401
import routes.tool_api.skills  # noqa: F401
import routes.tool_api.structure  # noqa: F401
from routes.tool_api._router import router  # noqa: E402

bind_tool_api_routes(router)
