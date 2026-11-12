"""SurrealDB client singleton and record serialization helpers.

Package split of the former backend/db.py: the full public surface is re-exported
here so every `from db import X` / `import db; db.X` site keeps resolving.

# SYSTEM: db-pool — SurrealDB connection singleton with auto-reconnect and record helpers
# ARCH: this package re-exports the API that lived in the flat backend/db.py. The
#       submodules are split by cohesion: pool (DBPool + timed proxy), contract
#       (transactions), records (serialization + id validation + record_refs),
#       crud (CRUD + projections), tree (traversal).
# ARCH: serialize_record adds _fmt datetime fields for frontend display (no client-side formatting).
"""

from __future__ import annotations

from db_schema import apply_schema, split_schema_statements  # noqa: F401

from db.contract import (  # noqa: F401
    _extract_query_raw_errors,
    run_in_transaction,
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
    tombstone_many,
)
from db.pool import (  # noqa: F401
    DBPool,
    SurrealReaderDiedError,
    _pool,
    _TimedDB,
    get_db,
    get_timed_proxy,
    mark_startup_complete,
    open_surreal,
    reset_db,
)
from db.records import (  # noqa: F401
    SAFE_ID_RE,
    _fmt_datetime,
    build_set_clause,
    coerce_record_ids,
    extract_id,
    is_record_id,
    iso_str,
    parse_iso,
    record_refs,
    serialize_record,
    validate_record_id,
)
from db.tree import get_ancestor_ids, get_descendant_ids  # noqa: F401
