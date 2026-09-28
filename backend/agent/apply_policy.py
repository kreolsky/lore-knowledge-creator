"""Unified agent apply-mode resolution.

# SYSTEM: apply-policy — single source of truth for whether an agent write
# applies directly (auto) or is held for the user's confirmation.

# ARCH: this replaces THREE drifting resolvers:
#   - the retired driver-client turn-level apply-mode resolver (Agent-line)
#   - tool_api._wants_auto / _can_auto_apply (Tool-API per-call)
#   - agent_config.system_doc_edit_policy  (system-doc override, edit-only)
# Unification is behavior-changing by design: there is no "intersection" that
# reproduces all three, so the precedence table below is the CHOSEN truth: the
# `is_system` cell applies uniformly to every write surface.

The pure resolver has no I/O.
"""
from __future__ import annotations

from dataclasses import dataclass

AUTO = "auto"
CONFIRM = "confirm"


@dataclass(frozen=True)
class ApplyDecision:
    """Result of resolve_apply_mode.

    `mode`   — "auto" (apply directly) or "confirm" (hold for a mid-turn verdict).
    `reason` — short label naming the rule that decided the turn, for tracing.
    """
    mode: str
    reason: str

    def __bool__(self) -> bool:  # noqa: D401
        """Truthy when the decision is auto-apply (convenient for `if decide:`)."""
        return self.mode == AUTO


def resolve_apply_mode(
    *,
    is_system: bool,
    ui_preference: str,
) -> ApplyDecision:
    """Pure precedence table — first match wins:

      1. is_system            → confirm (system docs never auto-apply)
      2. ui_preference == auto → auto
      3. else                 → confirm

    # INVARIANT(security): the only `auto` cell requires the user opted in AND the
    # target is not a system doc.
    # Why: system docs are the agent self-edit privilege path and always require
    # confirmation; the access-level cell this table once carried is gone — RBAC at
    # dispatch (the per-call gate) is the authority, so a non-full caller is refused
    # before this resolver ever runs.
    """
    if is_system:
        return ApplyDecision(CONFIRM, "system_doc")
    if ui_preference == AUTO:
        return ApplyDecision(AUTO, "opted_in")
    return ApplyDecision(CONFIRM, "ui_confirm")


__all__ = [
    "AUTO",
    "CONFIRM",
    "ApplyDecision",
    "resolve_apply_mode",
]
