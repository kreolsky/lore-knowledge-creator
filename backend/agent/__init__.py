"""The agent library — the non-route agent core under `backend/agent/`: keys,
apply policy, tool surface, edit-resolution + splice, read-only executors, and
the standalone CRDT apply path.

Packages that ARE routes (routes/chat, routes/tool_api) and the non-route
consumers (memory, scope, doc_edit_lock, turn_lock, driver.client, mcp_gateway)
share these primitives; none of them is a route, which is the boundary this
package exists to state.

The Agent line is driven by dsh over the Tool-API (completions.py routes via
turn hand-off), and user confirmation of a mutating call is the mid-turn
approval hold — the hold IS the delayed Tool-API response. Submodules:

  - context           : the Tool-API auth dependency (get_agent_context) + the
                        caller's own chat session (owned_session_row, pin).
  - keys              : agent-key minting + the per-session standing key (SYSTEM: agent-keys)
  - apply_policy      : the unified apply-mode resolver (SYSTEM: apply-policy)
  - tools             : the DERIVED agent view over backend/agent_tools (the tool
                        surface IS the agent's permission boundary; the ONE
                        declaration per tool lives in the agent_tools registry).
  - edit_primitives   : resolve_edit_range + _splice_edit (pure).
  - apply_edits_resolver: the shared edit core — resolve/validate/apply for the
                        batch edit path (edit + append + table-anchored edits) +
                        the pinned-region containment gate.
  - readonly_executors: read_document execution + read-only dispatch.
  - search_exec       : search_materials execution (two-layer + access filter).
  - structure_exec    : get_project_structure execution (BFS subtree).
  - table_writes      : the table mutation primitives (cell edits, rows, columns,
                        create) + route_tables_mutation convergence.
  - collab_writes     : create_document/create_reference via the live collab session.
  - doc_state         : live Y.Doc state, the remote-image guard, and the
                        content-convergence primitives (the write cluster's leaf).
  - tool_api_surface  : apply_edit_to_document (standalone CRDT apply path).
"""

# SYSTEM: chat-agent-mode — agent tool surface + apply primitives.
# Tool surface IS the agent's permission boundary.

# Facade-free: no side-effect submodule imports. `from agent import <submodule>`
# still resolves (the import system falls back to the submodule), and every
# consumer imports the owning module directly. None of the submodules binds a
# route — the package imports nothing from routes/.
