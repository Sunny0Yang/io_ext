"""io_ext policy exploration safety ledger.

Append-only, fail-visible record of policy exploration attempts and the
kernel/runtime safety events they produce.  See
``docs/experiments/iox_policy_safety_taxonomy.md`` for the category
definitions and the counting rules the derived summary follows.

Submodules:

- ``ledger``: attempt management (immutable manifests), event appending
  (flock-guarded JSONL), derived summaries (summary.json/summary.csv), and
  HALT flags for the upper-layer runner.

Importable as ``lib.iox_safety.*`` when ``PYTHONPATH`` includes ``src``
(the ioext-bench convention).
"""

from .ledger import (
    CONFIDENCES,
    DEFAULT_INCIDENT_WINDOW_S,
    EVENT_FIELDS,
    EVENT_TYPES,
    OUTCOME_FIELDS,
    RECOVERY_STATES,
    SEVERITIES,
    LedgerError,
    ManifestExistsError,
    SchemaError,
    UnknownAttemptError,
    append_event,
    attempt_dir,
    campaign_dir,
    create_attempt,
    derive_summary,
    events_path,
    finalize_attempt,
    halt_flag_path,
    list_attempt_ids,
    make_attempt_id,
    read_events,
    read_manifest,
    write_halt_flag,
)

__all__ = [
    "EVENT_TYPES",
    "SEVERITIES",
    "CONFIDENCES",
    "RECOVERY_STATES",
    "DEFAULT_INCIDENT_WINDOW_S",
    "EVENT_FIELDS",
    "OUTCOME_FIELDS",
    "LedgerError",
    "SchemaError",
    "ManifestExistsError",
    "UnknownAttemptError",
    "create_attempt",
    "read_manifest",
    "finalize_attempt",
    "append_event",
    "write_halt_flag",
    "read_events",
    "derive_summary",
    "list_attempt_ids",
    "make_attempt_id",
    "campaign_dir",
    "attempt_dir",
    "events_path",
    "halt_flag_path",
]
