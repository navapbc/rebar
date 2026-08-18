"""Recorded legacy exceptions for the config-ownership gate (RP-04 S7.1, ticket 29a9).

Each row suppresses ONE genuine current below-seam ambient read that predates the gate:
an exact ``path`` (relative to ``src/rebar``), the exact ``symbol`` (env-name / callee) the
gate reports, and a specific ``rationale`` naming the reading module, line, and access. This
is the ONLY sanctioned way to make the real tree pass — product code is never annotated to
silence the gate, and detection is never weakened. Retire a row by moving its read behind the
relevant composition-root or provider-credential seam.
"""

from __future__ import annotations

# (path, symbol, rationale) — expanded into the dict contract below.
_ROWS: list[tuple[str, str, str]] = [
    (
        "_cli/_audit_commands.py",
        "load_config",
        "_audit_commands.py L145: legacy load_config() call outside a composition root.",
    ),
    (
        "_cli/_init.py",
        "REBAR_ROOT",
        "_init.py L50/240: legacy env read of REBAR_ROOT.",
    ),
    (
        "_cli/_jira_onboard.py",
        "JIRA_PROJECT",
        "_jira_onboard.py L62: legacy env read of JIRA_PROJECT.",
    ),
    (
        "_cli/_jira_onboard.py",
        "JIRA_URL",
        "_jira_onboard.py L60: legacy env read of JIRA_URL.",
    ),
    (
        "_cli/_jira_onboard.py",
        "JIRA_USER",
        "_jira_onboard.py L61: legacy env read of JIRA_USER.",
    ),
    (
        "_cli/_jira_onboard.py",
        "load_config",
        "_jira_onboard.py L56: legacy load_config() call outside a composition root.",
    ),
    (
        "_cli/_jira_onboard.py",
        "os.environ.get",
        "_jira_onboard.py L63/191: legacy dynamic env read (os.environ.get).",
    ),
    (
        "_cli/_llm_eval_commands.py",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "_llm_eval_commands.py L260: legacy env read of OTEL_EXPORTER_OTLP_ENDPOINT.",
    ),
    (
        "_commands/_seam.py",
        "load_config",
        "_seam.py L352: legacy load_config() call outside a composition root.",
    ),
    (
        "_commands/bridge_repair.py",
        "USER",
        "bridge_repair.py L87: legacy env read of USER.",
    ),
    (
        "_commands/claim.py",
        "load_config",
        "claim.py L81: legacy load_config() call outside a composition root.",
    ),
    (
        "_commands/close_autoresume.py",
        "load_config",
        "close_autoresume.py L51: legacy load_config() call outside a composition root.",
    ),
    (
        "_commands/compact.py",
        "load_config",
        "compact.py L95/529: legacy load_config() call outside a composition root.",
    ),
    (
        "_commands/compact_trigger.py",
        "load_config",
        "compact_trigger.py L108/303: legacy load_config() call outside a composition root.",
    ),
    (
        "_commands/compact_txn.py",
        "REBAR_TEST_COMPACT_RENAME_BARRIER",
        "compact_txn.py L186: legacy env read of REBAR_TEST_COMPACT_RENAME_BARRIER.",
    ),
    (
        "_commands/composer.py",
        "REBAR_DETECTED_BY",
        "composer.py L254: legacy env read of REBAR_DETECTED_BY.",
    ),
    (
        "_commands/gates.py",
        "load_config",
        "gates.py L82/192: legacy load_config() call outside a composition root.",
    ),
    (
        "_commands/init.py",
        "REBAR_ROOT",
        "init.py L186: legacy env read of REBAR_ROOT.",
    ),
    (
        "_commands/metrics.py",
        "load_config",
        "metrics.py L139: legacy load_config() call outside a composition root.",
    ),
    (
        "_commands/remote_cert.py",
        "load_config",
        "remote_cert.py L36: legacy load_config() call outside a composition root.",
    ),
    (
        "_commands/scratch.py",
        "load_config",
        "scratch.py L37: legacy load_config() call outside a composition root.",
    ),
    (
        "_commands/session_id.py",
        "os.environ.get",
        "session_id.py L57: legacy dynamic env read (os.environ.get).",
    ),
    (
        "_commands/tracker_maintenance.py",
        "USER",
        "tracker_maintenance.py L132: legacy env read of USER.",
    ),
    (
        "_commands/verify_authorship.py",
        "load_config",
        "verify_authorship.py L509: legacy load_config() call outside a composition root.",
    ),
    (
        "_commands/verify_commit.py",
        "load_config",
        "verify_commit.py L171: legacy load_config() call outside a composition root.",
    ),
    (
        "_commands/verify_opcert.py",
        "load_config",
        "verify_opcert.py L151: legacy load_config() call outside a composition root.",
    ),
    (
        "_engine/rebar_reconciler/__main__.py",
        "REBAR_RECONCILER_LOCK_STEAL",
        "__main__.py L274: legacy env read of REBAR_RECONCILER_LOCK_STEAL.",
    ),
    (
        "_engine/rebar_reconciler/__main__.py",
        "REBAR_ROOT",
        "__main__.py L502/584: legacy env read of REBAR_ROOT.",
    ),
    (
        "_engine/rebar_reconciler/_advisory_lock.py",
        "load_config",
        "_advisory_lock.py L69/87: legacy load_config() call outside a composition root.",
    ),
    (
        "_engine/rebar_reconciler/_preflight.py",
        "load_config",
        "_preflight.py L86: legacy load_config() call outside a composition root.",
    ),
    (
        "_engine/rebar_reconciler/access_check.py",
        "JIRA_PROJECT",
        "access_check.py L76/286: legacy env read of JIRA_PROJECT.",
    ),
    (
        "_engine/rebar_reconciler/access_check.py",
        "JIRA_URL",
        "access_check.py L73/283: legacy env read of JIRA_URL.",
    ),
    (
        "_engine/rebar_reconciler/access_check.py",
        "JIRA_USER",
        "access_check.py L74/284: legacy env read of JIRA_USER.",
    ),
    (
        "_engine/rebar_reconciler/adapters/jira/acli_subprocess.py",
        "JIRA_PROJECT",
        "acli_subprocess.py L129: legacy env read of JIRA_PROJECT.",
    ),
    (
        "_engine/rebar_reconciler/adapters/jira/acli_subprocess.py",
        "JIRA_URL",
        "acli_subprocess.py L127: legacy env read of JIRA_URL.",
    ),
    (
        "_engine/rebar_reconciler/adapters/jira/acli_subprocess.py",
        "JIRA_USER",
        "acli_subprocess.py L128: legacy env read of JIRA_USER.",
    ),
    (
        "_engine/rebar_reconciler/adapters/jira/acli_subprocess.py",
        "load_config",
        "acli_subprocess.py L86/119: legacy load_config() call outside a composition root.",
    ),
    (
        "_engine/rebar_reconciler/adapters/jira/outbound_fields.py",
        "REBAR_RECONCILER_VERBOSE",
        "outbound_fields.py L412: legacy env read of REBAR_RECONCILER_VERBOSE.",
    ),
    (
        "_engine/rebar_reconciler/adapters/jira/outbound_fields.py",
        "os.environ.get",
        "outbound_fields.py L49: legacy dynamic env read (os.environ.get).",
    ),
    (
        "_engine/rebar_reconciler/adapters/jira_datacenter/settings.py",
        "load_config",
        "settings.py L83/97: legacy load_config() call outside a composition root.",
    ),
    (
        "_engine/rebar_reconciler/adapters/jira_family/rich_text.py",
        "load_config",
        "rich_text.py L90: legacy load_config() call outside a composition root.",
    ),
    (
        "_engine/rebar_reconciler/adapters/jira_family/wiki_render.py",
        "load_config",
        "wiki_render.py L352: legacy load_config() call outside a composition root.",
    ),
    (
        "_engine/rebar_reconciler/applier.py",
        "REBAR_ROOT",
        "applier.py L576: legacy env read of REBAR_ROOT.",
    ),
    (
        "_engine/rebar_reconciler/applier.py",
        "load_config",
        "applier.py L210/613: legacy load_config() call outside a composition root.",
    ),
    (
        "_engine/rebar_reconciler/apply_handlers.py",
        "REBAR_RECONCILER_FAIL_SILENT_NOOP",
        "apply_handlers.py L184/424/462: legacy env read of REBAR_RECONCILER_FAIL_SILENT_NOOP.",
    ),
    (
        "_engine/rebar_reconciler/apply_handlers.py",
        "load_config",
        "apply_handlers.py L214: legacy load_config() call outside a composition root.",
    ),
    (
        "_engine/rebar_reconciler/apply_handlers.py",
        "os.environ.get",
        "apply_handlers.py L64: legacy dynamic env read (os.environ.get).",
    ),
    (
        "_engine/rebar_reconciler/apply_inbound.py",
        "REBAR_RECONCILER_CONFLICT_PARENT_ID",
        "apply_inbound.py L244: legacy env read of REBAR_RECONCILER_CONFLICT_PARENT_ID.",
    ),
    (
        "_engine/rebar_reconciler/apply_inbound.py",
        "REBAR_ROOT",
        "apply_inbound.py L106: legacy env read of REBAR_ROOT.",
    ),
    (
        "_engine/rebar_reconciler/apply_inbound.py",
        "os.environ.get",
        "apply_inbound.py L62: legacy dynamic env read (os.environ.get).",
    ),
    (
        "_engine/rebar_reconciler/apply_inbound_records.py",
        "load_config",
        "apply_inbound_records.py L133: legacy load_config() call outside a composition root.",
    ),
    (
        "_engine/rebar_reconciler/apply_planning.py",
        "REBAR_ROOT",
        "apply_planning.py L267: legacy env read of REBAR_ROOT.",
    ),
    (
        "_engine/rebar_reconciler/binding_store.py",
        "RECONCILER_ABSENT_RETIRE_GRACE",
        "binding_store.py L538: legacy env read of RECONCILER_ABSENT_RETIRE_GRACE.",
    ),
    (
        "_engine/rebar_reconciler/binding_store.py",
        "os.environ.get",
        "binding_store.py L110: legacy dynamic env read (os.environ.get).",
    ),
    (
        "_engine/rebar_reconciler/binding_walk.py",
        "RECONCILER_ABSENT_RETIRE_GRACE",
        "binding_walk.py L89: legacy env read of RECONCILER_ABSENT_RETIRE_GRACE.",
    ),
    (
        "_engine/rebar_reconciler/dispatch_one.py",
        "REBAR_ROOT",
        "dispatch_one.py L157/277: legacy env read of REBAR_ROOT.",
    ),
    (
        "_engine/rebar_reconciler/fetcher.py",
        "REBAR_ROOT",
        "fetcher.py L507/626: legacy env read of REBAR_ROOT.",
    ),
    (
        "_engine/rebar_reconciler/fetcher.py",
        "load_config",
        "fetcher.py L122/520: legacy load_config() call outside a composition root.",
    ),
    (
        "_engine/rebar_reconciler/inbound_differ.py",
        "load_config",
        "inbound_differ.py L139/637: legacy load_config() call outside a composition root.",
    ),
    (
        "_engine/rebar_reconciler/inbound_translate.py",
        "REBAR_AUTHOR",
        "inbound_translate.py L175: legacy env read of REBAR_AUTHOR.",
    ),
    (
        "_engine/rebar_reconciler/inbound_translate.py",
        "REBAR_ENV_ID",
        "inbound_translate.py L174: legacy env read of REBAR_ENV_ID.",
    ),
    (
        "_engine/rebar_reconciler/inbound_translate.py",
        "REBAR_ROOT",
        "inbound_translate.py L187: legacy env read of REBAR_ROOT.",
    ),
    (
        "_engine/rebar_reconciler/inbound_translate.py",
        "load_config",
        "inbound_translate.py L355: legacy load_config() call outside a composition root.",
    ),
    (
        "_engine/rebar_reconciler/inbound_translate.py",
        "os.environ.get",
        "inbound_translate.py L50: legacy dynamic env read (os.environ.get).",
    ),
    (
        "_engine/rebar_reconciler/invariants.py",
        "REBAR_ROOT",
        "invariants.py L20: legacy env read of REBAR_ROOT.",
    ),
    (
        "_engine/rebar_reconciler/last_pass.py",
        "REBAR_ENV_ID",
        "last_pass.py L71/329: legacy env read of REBAR_ENV_ID.",
    ),
    (
        "_engine/rebar_reconciler/last_pass.py",
        "load_config",
        "last_pass.py L53: legacy load_config() call outside a composition root.",
    ),
    (
        "_engine/rebar_reconciler/outbound_comments.py",
        "load_config",
        "outbound_comments.py L92/103/149: legacy load_config() call outside a composition root.",
    ),
    (
        "_engine/rebar_reconciler/outbound_differ.py",
        "load_config",
        "outbound_differ.py L344/492: legacy load_config() call outside a composition root.",
    ),
    (
        "_engine/rebar_reconciler/outbound_differ.py",
        "os.environ.get",
        "outbound_differ.py L89/114: legacy dynamic env read (os.environ.get).",
    ),
    (
        "_engine/rebar_reconciler/pass_io.py",
        "REBAR_ROOT",
        "pass_io.py L109/164: legacy env read of REBAR_ROOT.",
    ),
    (
        "_engine/rebar_reconciler/rebar_id_audit.py",
        "load_config",
        "rebar_id_audit.py L157: legacy load_config() call outside a composition root.",
    ),
    (
        "_engine/rebar_reconciler/rebar_id_audit.py",
        "os.environ.get",
        "rebar_id_audit.py L30: legacy dynamic env read (os.environ.get).",
    ),
    (
        "_engine/rebar_reconciler/reconcile.py",
        "REBAR_ROOT",
        "reconcile.py L210: legacy env read of REBAR_ROOT.",
    ),
    (
        "_engine/rebar_reconciler/reconcile_check.py",
        "load_config",
        "reconcile_check.py L305: legacy load_config() call outside a composition root.",
    ),
    (
        "_engine/rebar_reconciler/reconcile_helpers.py",
        "REBAR_RECONCILER_WRITE_FACADE",
        "reconcile_helpers.py L629: legacy env read of REBAR_RECONCILER_WRITE_FACADE.",
    ),
    (
        "_engine/rebar_reconciler/request.py",
        "REBAR_ROOT",
        "request.py L120: legacy env read of REBAR_ROOT.",
    ),
    (
        "_engine/rebar_reconciler/run_differs.py",
        "load_config",
        "run_differs.py L78/688: legacy load_config() call outside a composition root.",
    ),
    (
        "_engine_support/bridge_fsck_visibility.py",
        "os.environ.get",
        "bridge_fsck_visibility.py L74: legacy dynamic env read (os.environ.get).",
    ),
    (
        "_engine_support/gates.py",
        "load_config",
        "gates.py L150: legacy load_config() call outside a composition root.",
    ),
    (
        "_engine_support/lookups.py",
        "load_config",
        "lookups.py L72: legacy load_config() call outside a composition root.",
    ),
    (
        "_engine_support/reads.py",
        "REBAR_ROOT",
        "reads.py L125: legacy env read of REBAR_ROOT.",
    ),
    (
        "_engine_support/reads.py",
        "load_config",
        "reads.py L118/185/345: legacy load_config() call outside a composition root.",
    ),
    (
        "_io/import_ndjson.py",
        "REBAR_SYNC_PUSH",
        "import_ndjson.py L216/217/387: legacy env read of REBAR_SYNC_PUSH.",
    ),
    (
        "_logging.py",
        "REBAR_LOG_LEVEL",
        "_logging.py L93: legacy env read of REBAR_LOG_LEVEL.",
    ),
    (
        "_mcp_auth.py",
        "os.environ.get",
        "_mcp_auth.py L411/658/760: legacy dynamic env read (os.environ.get).",
    ),
    (
        "_opcert_signing.py",
        "REBAR_OPCERT_ENV_ID",
        "_opcert_signing.py L222: legacy env read of REBAR_OPCERT_ENV_ID.",
    ),
    (
        "_opcert_signing.py",
        "REBAR_OPCERT_KEY_PATH",
        "_opcert_signing.py L188: legacy env read of REBAR_OPCERT_KEY_PATH.",
    ),
    (
        "_operation_config.py",
        "os.environ.get",
        "_operation_config.py L54: legacy dynamic env read (os.environ.get).",
    ),
    (
        "_snapshot/git_fetch.py",
        "REBAR_SNAPSHOT_STALL_ATTEMPTS",
        "git_fetch.py L274: legacy env read of REBAR_SNAPSHOT_STALL_ATTEMPTS.",
    ),
    (
        "_snapshot/git_fetch.py",
        "REBAR_SNAPSHOT_STALL_FLOOR_BYTES_PER_SEC",
        "git_fetch.py L218: legacy env read of REBAR_SNAPSHOT_STALL_FLOOR_BYTES_PER_SEC.",
    ),
    (
        "_snapshot/git_fetch.py",
        "REBAR_SNAPSHOT_STALL_WINDOW_SECONDS",
        "git_fetch.py L222: legacy env read of REBAR_SNAPSHOT_STALL_WINDOW_SECONDS.",
    ),
    (
        "_snapshot/janitor.py",
        "REBAR_GATE_FREE_WATERMARK_BYTES",
        "janitor.py L106: legacy env read of REBAR_GATE_FREE_WATERMARK_BYTES.",
    ),
    (
        "_snapshot/janitor.py",
        "REBAR_GATE_GRACE_SECONDS",
        "janitor.py L112: legacy env read of REBAR_GATE_GRACE_SECONDS.",
    ),
    (
        "_snapshot/janitor.py",
        "REBAR_GATE_JANITOR_INTERVAL_SECONDS",
        "janitor.py L121: legacy env read of REBAR_GATE_JANITOR_INTERVAL_SECONDS.",
    ),
    (
        "_snapshot/janitor.py",
        "REBAR_GATE_MAX_AGE_SECONDS",
        "janitor.py L115: legacy env read of REBAR_GATE_MAX_AGE_SECONDS.",
    ),
    (
        "_snapshot/janitor.py",
        "REBAR_GATE_REVERIFY_SECONDS",
        "janitor.py L118: legacy env read of REBAR_GATE_REVERIFY_SECONDS.",
    ),
    (
        "_snapshot/janitor.py",
        "os.environ.get",
        "janitor.py L73: legacy dynamic env read (os.environ.get).",
    ),
    (
        "_snapshot/repo_snapshot.py",
        "REBAR_GATE_TMPDIR",
        "repo_snapshot.py L119: legacy env read of REBAR_GATE_TMPDIR.",
    ),
    (
        "_store/ensures.py",
        "load_config",
        "ensures.py L245: legacy load_config() call outside a composition root.",
    ),
    (
        "_store/env_identity.py",
        "REBAR_ALLOW_ENV_REIDENTIFY",
        "env_identity.py L109: legacy env read of REBAR_ALLOW_ENV_REIDENTIFY.",
    ),
    (
        "_store/hlc.py",
        "REBAR_HLC",
        "hlc.py L61: legacy env read of REBAR_HLC.",
    ),
    (
        "_store/hlc.py",
        "REBAR_HLC_NOW",
        "hlc.py L71: legacy env read of REBAR_HLC_NOW.",
    ),
    (
        "_store/lock.py",
        "REBAR_LOCK_RETRIES",
        "lock.py L419: legacy env read of REBAR_LOCK_RETRIES.",
    ),
    (
        "_store/project_ensures.py",
        "load_config",
        "project_ensures.py L69: legacy load_config() call outside a composition root.",
    ),
    (
        "_store/push.py",
        "load_config",
        "push.py L63: legacy load_config() call outside a composition root.",
    ),
    (
        "grounding/harness.py",
        "os.environ.get",
        "harness.py L56: legacy dynamic env read (os.environ.get).",
    ),
    (
        "grounding/oracle.py",
        "load_config",
        "oracle.py L170: legacy load_config() call outside a composition root.",
    ),
    (
        "grounding/resolve.py",
        "REBAR_CTAGS_BIN",
        "resolve.py L62: legacy import-time default capture of REBAR_CTAGS_BIN.",
    ),
    (
        "grounding/resolve.py",
        "load_config",
        "resolve.py L543: legacy load_config() call outside a composition root.",
    ),
    (
        "llm/code_review/workflow_ops.py",
        "load_config",
        "workflow_ops.py L664: legacy load_config() call outside a composition root.",
    ),
    (
        "llm/completion.py",
        "load_config",
        "completion.py L215: legacy load_config() call outside a composition root.",
    ),
    (
        "llm/enrich_drain.py",
        "load_config",
        "enrich_drain.py L303: legacy load_config() call outside a composition root.",
    ),
    (
        "llm/gate_context.py",
        "REBAR_GATE_ALLOW_UNGATED",
        "gate_context.py L201: legacy env read of REBAR_GATE_ALLOW_UNGATED.",
    ),
    (
        "llm/gate_source.py",
        "REBAR_GATE_REF",
        "gate_source.py L81: legacy env read of REBAR_GATE_REF.",
    ),
    (
        "llm/gate_source.py",
        "REBAR_GATE_SOURCE",
        "gate_source.py L87: legacy env read of REBAR_GATE_SOURCE.",
    ),
    (
        "llm/gate_source.py",
        "os.environ.get",
        "gate_source.py L70: legacy dynamic env read (os.environ.get).",
    ),
    (
        "llm/plan_review/__init__.py",
        "load_config",
        "__init__.py L99/216/374/774: legacy load_config() call outside a composition root.",
    ),
    (
        "llm/plan_review/det_floor.py",
        "load_config",
        "det_floor.py L376: legacy load_config() call outside a composition root.",
    ),
    (
        "llm/plan_review/drift_floor.py",
        "load_config",
        "drift_floor.py L171/310: legacy load_config() call outside a composition root.",
    ),
    (
        "llm/plan_review/pin_health.py",
        "load_config",
        "pin_health.py L85: legacy load_config() call outside a composition root.",
    ),
    (
        "llm/plan_review/sizing.py",
        "REBAR_PLAN_REVIEW_BUDGET",
        "sizing.py L125: legacy env read of REBAR_PLAN_REVIEW_BUDGET.",
    ),
    (
        "llm/plan_review/workflow_ops.py",
        "load_config",
        "workflow_ops.py L352: legacy load_config() call outside a composition root.",
    ),
    (
        "llm/plan_review/xcheck.py",
        "load_config",
        "xcheck.py L236/384: legacy load_config() call outside a composition root.",
    ),
    (
        "llm/usage_log.py",
        "REBAR_USAGE_LOG",
        "usage_log.py L472: legacy env read of REBAR_USAGE_LOG.",
    ),
    (
        "llm/workflow/completion_banking.py",
        "REBAR_ROOT",
        "completion_banking.py L450: legacy env read of REBAR_ROOT.",
    ),
    (
        "llm/workflow/completion_banking.py",
        "load_config",
        "completion_banking.py L118: legacy load_config() call outside a composition root.",
    ),
    (
        "llm/workflow/criterion_preview.py",
        "REBAR_PREVIEW_TIMEOUT",
        "criterion_preview.py L181: legacy env read of REBAR_PREVIEW_TIMEOUT.",
    ),
    (
        "llm/workflow/executor.py",
        "os.environ[...]",
        "executor.py L442: legacy dynamic env read (os.environ[...]).",
    ),
    (
        "llm/workflow/gate_dispatch.py",
        "load_config",
        "gate_dispatch.py L278: legacy load_config() call outside a composition root.",
    ),
    (
        "llm/workflow/gate_ops.py",
        "REBAR_VERIFY_PREFETCH",
        "gate_ops.py L196: legacy env read of REBAR_VERIFY_PREFETCH.",
    ),
    (
        "llm/workflow/gate_ops.py",
        "load_config",
        "gate_ops.py L79: legacy load_config() call outside a composition root.",
    ),
    (
        "llm/workflow/interpreter.py",
        "os.environ[...]",
        "interpreter.py L206: legacy dynamic env read (os.environ[...]).",
    ),
    (
        "mcp_server.py",
        "load_config",
        "mcp_server.py L422/591/735: legacy load_config() call outside a composition root.",
    ),
    (
        "mcp_server.py",
        "os.environ.get",
        "mcp_server.py L646: legacy dynamic env read (os.environ.get).",
    ),
    (
        "mirror_guard.py",
        "GITHUB_TOKEN",
        "mirror_guard.py L231: legacy env read of GITHUB_TOKEN.",
    ),
    (
        "review_bot/app.py",
        "REVIEW_BOT_PORT",
        "app.py L477: legacy env read of REVIEW_BOT_PORT.",
    ),
    (
        "signing.py",
        "REBAR_SIGNING_KEY",
        "signing.py L98: legacy env read of REBAR_SIGNING_KEY.",
    ),
]

LEGACY_EXCEPTIONS: list[dict] = [
    {"path": path, "symbol": symbol, "rationale": rationale} for path, symbol, rationale in _ROWS
]
