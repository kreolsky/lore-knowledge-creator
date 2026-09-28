"""Tool-API sandbox domain — arbitrary bash on the agent's console host.

# SYSTEM: agent-sandbox — the agent's console: an SSH host with Python where
#   `sandbox_bash` runs. Dev = the `sandbox` compose service; prod = LXC CT 703 on
#   black. Provisioning + the egress policy: docs/sandbox-access.md.

# ARCH: this domain maps a tool call onto an SSH command and nothing else. There is
# no sandbox manager, no container lifecycle, no registry table — the far end is a
# host that already exists and the filesystem IS the state. The backend knows only
# SANDBOX_SSH_HOST + a key + a path; swapping dev for prod is an env change.
# The write path deliberately does NOT touch apply_policy: `sandbox_bash` is in
# MUTATING_TOOLS (which buys sequential execution in the dsh driver — one console, one
# command at a time), but proposal/confirm is resolved by the DOCUMENT endpoints via
# resolve_apply_mode, not by tool-surface membership. Bash always executes directly;
# there is nothing to diff and nothing to roll back.

# ARCH: a third mode rides the SAME tool: `sandbox_bash(detach=true)` starts the
# command in the background and returns {status:"running", run_id} at once; a
# separate READ-ONLY tool (`sandbox_run_status`) polls the run. The run's state is
# `.runs/{run_id}/` under the caller's workspace — files, not a registry — so it
# survives a backend restart for free. Detached runs make the workspace CONCURRENT
# for the first time (one HOME, one venv, one file tree): documented, not mitigated.

Facade-free by decision (plan fewer-layers): this package __init__ holds the
docstring + markers only. Owners: `transport` (workspace mapping, SSH/SFTP
primitives, exec caps, the config gate, all timeout/limit constants),
`files` (file-bridge primitives + console-key/access guards), `runs` (detached
runs + run status), `bash` (the console tool handlers), `lifecycle` (workspace
destroy/restart/status). Consumers import the owning submodule (routes/sandbox.py
→ `sandbox.lifecycle`; the registry paths → `sandbox.bash` / `sandbox.runs`).
Test-seam monkeypatches target the submodule that RESOLVES the seam.
"""
