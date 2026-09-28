"""Chat sessions — the command layer for session create / update / delete.

# ARCH: routes/chat/sessions.py handlers stay thin — the entity access guard, then
#   a `*_command` from this package — the same shape as routes/documents.py over
#   `backend/documents/`. What mutates a session row and what it triggers on the
#   way out (LLM-config inheritance, the note-link strip, note realtime nudges, the
#   wire serialization) lives here. Field-level access that depends on the body
#   (the agent_auto clamp, the reparent target) stays with its field in the
#   command, so a request's error precedence is the field order. What reaches the
#   harness (the parked-ask relay, the fan-out stop) stays in the route: it is
#   route-resident code, and nothing outside routes/ imports routes.

Facade-free: consumers import the OWNING module — `chat_sessions.serialize`
(session wire shape + ref map), `chat_sessions.note_events` (note broadcast
target + fire-and-forget scheduling), `chat_sessions.create` / `.update` /
`.delete` (the commands).
"""
