"""The driver seam — Lore's one package for everything that talks to the
agent-line driver service (SYSTEM: driver-client, entry client.py; the
compaction row-mint half, SYSTEM: compaction, entry compaction.py).

Owns: the line descriptor + capability answer + turn payload builder
(client.py), the standing backend→driver event channel (channel.py), the
relay arms + turn projection (frames.py), the timeline fetchers + turn RPCs
(timeline.py), the turn-row persistence writes (persistence.py), and the
compaction continuation mint (compaction.py). Routes stay in
routes/chat/completions*.py and fanout.py and import the owning module —
this package exports nothing.
"""
