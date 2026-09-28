"""SurrealDB client singleton and record serialization helpers.

Package split of the former backend/db.py — behavior-preserving: the full public
surface is re-exported here so every `from db import X` / `import db; db.X` site
keeps resolving, and the surrealdb _recv_task monkeypatch remains a side effect of
`import db`.

# SYSTEM: db-pool — SurrealDB connection singleton with auto-reconnect and record helpers
# ARCH: this package re-exports the API that lived in the flat backend/db.py. The
#       submodules are split by cohesion: _patch (recv-task patch + id validation),
#       pool (DBPool + timed proxy), contract (SDK contract detectors + transactions),
#       records (serialization), crud (CRUD + projections), tree (traversal).
# ARCH: import order is load-bearing — db._patch is imported FIRST so the
#       _recv_task monkeypatch (fut.done() guard) is installed before any get_db()
#       call can wedge the SDK reader. _patch also captures _ORIG_RECV_TASK (the
#       pre-patch reader), which db.contract binds as the default arg of
#       _assert_recv_task_patched; importing contract after _patch guarantees the
#       default captures the pre-patch value.
# ARCH: serialize_record adds _fmt datetime fields for frontend display (no client-side formatting).
"""

from __future__ import annotations

from db_schema import apply_schema, split_schema_statements  # noqa: F401

# WHY import order: _patch installs the surrealdb _recv_task monkeypatch (fut.done()
# guard) at import time. It MUST run before pool/contract so any get_db() hits a
# patched reader, and before contract so _ORIG_RECV_TASK is captured for the
# _assert_recv_task_patched default argument.
from db._patch import (  # noqa: F401
    _ORIG_RECV_TASK,
    SAFE_ID_RE,
    _AsyncWsConn,
    _patched_recv_task,
    validate_record_id,
)
from db.contract import (  # noqa: F401
    SdkContractError,
    _assert_query_raises_on_error,
    _assert_query_raw_envelope,
    _assert_record_id_name,
    _assert_recv_task_patched,
    _extract_query_raw_errors,
    run_in_transaction,
    verify_sdk_contract,
)
from db.crud import (  # noqa: F401
    _DOC_PROJECTIONS,
    DOC_BATCH_DELETE_COLUMNS,
    DOC_META_COLUMNS,
    DOC_TRANSCLUSION_COLUMNS,
    REF_META_COLUMNS,
    create_record,
    fetch_doc_meta,
    fetch_many,
    fetch_one,
    soft_delete,
)
from db.pool import (  # noqa: F401
    DBPool,
    _pool,
    _TimedDB,
    get_db,
    get_timed_proxy,
    mark_startup_complete,
    reset_db,
)
from db.records import (  # noqa: F401
    _fmt_datetime,
    coerce_record_ids,
    extract_id,
    is_record_id,
    serialize_record,
)
from db.tree import get_ancestor_ids, get_descendant_ids  # noqa: F401
