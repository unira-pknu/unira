"""One CLI for selective recovery; phase handlers retain their original review guards."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from dataclasses import dataclass
import importlib
from pathlib import Path
import sys

from scripts.recovery.common import atomic, event, inside, read, receipt
from scripts.vectorization.recovery_control import LoadOptions, recovery_lock, resume_plan

ROOT = Path(__file__).resolve().parents[2]
BACKEND = Path("D:/unira-backend/backend")
PROBE_QUESTION = "2026학년도 2학기 학부 유승호 교수님이 진행하는 강의명을 알려줘"


@dataclass(frozen=True)
class Task:
    module: str
    phases: tuple[str, ...]
    initial: str
    options: tuple[str, ...] = ()
    lock: str = ".lock"
    policy: object = None
    plan_file: str = "plan.json"


TASKS = {
    "zip": Task("scripts.preprocessing.repair_archive_collisions",
                ("prepare", "stage", "vectorize", "preflight", "resume", "apply", "verify", "publish"),
                "prepare", ("after_receipt",), policy=2),
    "formats": Task("scripts.preprocessing.repair_attachment_formats", ("scan", "process"), "scan",
                    ("recover", "retry_failed", "file_ext"), policy="attachment-signature-v1"),
    "rules": Task("scripts.preprocessing.repair_rule_sources",
                  ("prepare", "vectorize", "apply", "publish", "verify"), "prepare",
                  ("after_receipt", "backend_root"), policy="rule-provenance-v1"),
    "ce": Task("scripts.vectorization.reconcile_ce",
               ("compare", "probe", "stage", "apply", "publish", "verify"), "compare",
               ("expected_count", "after_receipt", "backend_root"), policy="ce-original-reconciliation-v1"),
    "excel": Task("scripts.vectorization.resume_excel", ("inventory", "load", "verify", "finalize"),
                  "inventory", ("workers", "chunk_batch_size", "backend_root", "probe_question", "probe_preprocessed"),
                  lock=".excel_resume.lock", policy="excel-verified-resume-v1", plan_file="resume_inventory.json"),
    "publish": Task("scripts.vectorization.publish_recovery",
                    ("prepare", "vectorize", "apply", "publish", "verify"), "prepare",
                    ("source_run", "kind", "backend_root", "after_receipt"), policy="scoped-recovery-publication-v1"),
    "quality": Task("scripts.preprocessing.review_text_quality", ("scan", "probe", "stage"), "scan",
                    ("expected_chunks", "expected_pdfs", "pdf_source_id", "ocr", "retry_errors", "max_ocr_pages"),
                    policy="original-text-quality-review-v1"),
    "sources": Task("scripts.vectorization.reconcile_active_sources", ("scan", "baseline", "apply", "verify"),
                    "scan", ("expected_db_only", "expected_groups", "after_receipt", "backend_root"),
                    policy="active-source-exact-duplicates-v1"),
    "missing": Task("scripts.recovery.missing", ("prepare", "load", "priorities", "verify", "search", "resume"),
                    "prepare", (*LoadOptions.__dataclass_fields__, "backend_root", "cases_per_family"),
                    lock=".phase.lock", plan_file="missing_sources.json"),
}


def parser():
    p = argparse.ArgumentParser(description=__doc__, argument_default=argparse.SUPPRESS)
    p.add_argument("--list", action="store_true", help="List tasks/phases without imports or jobs")
    p.add_argument("--task", choices=TASKS)
    p.add_argument("--run", type=Path)
    p.add_argument("--phase", nargs="+", help="Explicit sequential phases; defaults to local status")
    p.add_argument("--plan", action="store_true", help="Preview only; no models, database or file writes")
    p.add_argument("--after-receipt", type=Path, action="append")
    p.add_argument("--backend-root", type=Path)
    p.add_argument("--source-run", type=Path)
    p.add_argument("--kind", choices=("formats", "quality"))
    for option in ("recover", "retry-failed", "ocr", "retry-errors"):
        p.add_argument("--" + option, action="store_true")
    p.add_argument("--file-ext", action="append")
    p.add_argument("--pdf-source-id", action="append")
    for name in (*LoadOptions.__dataclass_fields__, "expected_count", "expected_chunks", "expected_pdfs",
                 "expected_db_only", "expected_groups", "max_ocr_pages", "cases_per_family"):
        p.add_argument("--" + name.replace("_", "-"), type=int)
    p.add_argument("--probe-question")
    p.add_argument("--probe-preprocessed", type=Path)
    return p


def options_for(task, explicit):
    invalid = set(explicit) - set(TASKS[task].options) - {"task", "run", "phase", "plan", "list"}
    if invalid:
        raise ValueError(f"Options do not apply to {task}: " + ", ".join(sorted(invalid)))
    values = dict(backend_root=BACKEND, after_receipt=[], recover=False, retry_failed=False, file_ext=[],
                  expected_count=4, expected_chunks=101, expected_pdfs=3, pdf_source_id=[], ocr=False,
                  retry_errors=False, max_ocr_pages=32, expected_db_only=34, expected_groups=16,
                  cases_per_family=6, source_run=None, kind=None, probe_question=PROBE_QUESTION,
                  probe_preprocessed=None, **vars(LoadOptions()))
    if task == "excel":
        values.update(workers=2, chunk_batch_size=256)
    values.update(explicit)
    if task == "excel":
        if values["workers"] not in (1, 2) or not 1 <= values["chunk_batch_size"] <= 512:
            raise ValueError("Excel requires --workers 1..2 and --chunk-batch-size 1..512")
    elif task == "missing":
        LoadOptions(**{k: values[k] for k in LoadOptions.__dataclass_fields__})
    for key in ("expected_count", "max_ocr_pages", "cases_per_family"):
        if values[key] < 1:
            raise ValueError(f"{key} must be positive")
    for key in ("expected_chunks", "expected_pdfs", "expected_db_only", "expected_groups"):
        if values[key] < 0:
            raise ValueError(f"{key} must not be negative")
    if values["cases_per_family"] > 12:
        raise ValueError("--cases-per-family must be 1..12")
    values["backend_root"] = values["backend_root"].resolve()
    values["after_receipt"] = [inside(v, root=ROOT) for v in values["after_receipt"]]
    if values["source_run"]:
        values["source_run"] = run_path(values["source_run"])
    return argparse.Namespace(**values)


def run_path(path):
    boundary = (ROOT / "var/recovery").resolve()
    run = inside(path, root=ROOT, boundary=boundary)
    if run == boundary:
        raise ValueError("Select an individual run directory under var/recovery")
    if run.exists() and not run.is_dir():
        raise ValueError("--run must be a directory")
    return run


def validate_run(task, run, phases):
    """Reject foreign run directories before imports, locks or receipt changes."""
    marker = run / "recovery_job.json"
    if marker.exists() and (read(marker).get("task") != task or read(marker).get("version") != 1):
        raise ValueError("This run belongs to another recovery task/version")
    found = set()
    for filename in ("plan.json", "resume_inventory.json"):
        path = run / filename
        if path.exists():
            plan = read(path)
            policy = plan.get("policy", plan.get("policy_version"))
            matching = {key for key, spec in TASKS.items() if spec.policy == policy and spec.plan_file == filename}
            if not matching:
                raise ValueError(f"Unrecognized recovery plan: {path}")
            found.update(matching)
    if (run / "missing_sources.json").exists():
        found.add("missing")
    state = read(run / "receipt.json") if (run / "receipt.json").exists() else {}
    if state.get("mode") == "insert_missing_only":
        found.add("missing")
    if (run / "targets.json").exists() and "inventory" in state and "preprocessing" in state:
        found.add("excel")
    if found and found != {task}:
        raise ValueError("This directory contains a different recovery job")
    lock_names = {".lock", ".phase.lock", ".pipeline.lock", ".excel_resume.lock"}
    if not found and not marker.exists() and run.exists() and any(p.name not in lock_names for p in run.iterdir()):
        raise ValueError("Nonempty run has no recognized identity; choose a new directory")
    if phases == ["status"]:
        return
    if state.get("superseded_by"):
        raise ValueError(f"This run was superseded; use {state['superseded_by']}")
    spec = TASKS[task]
    plan_exists = (run / spec.plan_file).exists()
    if phases[0] == spec.initial:
        if plan_exists and task != "missing":
            raise ValueError("Plan already exists; reuse its next phase or select a new run")
        if task == "excel" and not (run / "targets.json").exists():
            raise ValueError("Excel resume requires an existing rebuild with targets.json")
    elif not plan_exists:
        raise ValueError(f"Run {spec.initial} first; {spec.plan_file} is absent")


def missing_remaining(run):
    selected = read(run / "missing_sources.json")
    expected = {entry["id"] for entry in selected}
    if len(expected) != len(selected):
        raise ValueError("Confirmed selection contains duplicate source IDs")
    state = read(run / "receipt.json") if (run / "receipt.json").exists() else {}
    progress = read(run / "load.json") if (run / "load.json").exists() else {}
    return resume_plan(state, progress, expected)


def dispatch(task, phase, module, run, a):
    """Domain algorithms stay independent; orchestration and options live here."""
    if task == "missing":
        if phase == "search":
            verifier = importlib.import_module("scripts.vectorization.verify_recovery_search")
            return verifier.verify(run, a.backend_root, a.cases_per_family)
        previous = module.RUN
        module.RUN = run
        try:
            if phase == "load":
                return module.load(LoadOptions(**{k: getattr(a, k) for k in LoadOptions.__dataclass_fields__}))
            return getattr(module, phase)()
        finally:
            module.RUN = previous
    if task == "zip":
        if phase == "stage":
            return module.stage(run, read(run / "plan.json"))
        if phase in {"apply", "publish"}:
            return getattr(module, phase)(run, read(run / "plan.json"), a.after_receipt)
        if phase == "resume":
            return module.resume(run, a.after_receipt)
    elif task == "formats":
        if phase == "scan":
            return module.scan(run, {"." + e.lower().lstrip(".") for e in a.file_ext} or None)
        return module.process(run, a.recover, a.retry_failed)
    elif task == "quality":
        if phase == "scan":
            return module.scan(run, a.expected_chunks, a.expected_pdfs, set(a.pdf_source_id))
        if phase == "probe":
            return module.probe(run, a.ocr, a.max_ocr_pages, a.retry_errors)
    elif task == "ce":
        if phase == "compare":
            return module.compare(run, a.expected_count)
        if phase in {"apply", "publish"}:
            return getattr(module, phase)(run, a.after_receipt)
        if phase == "verify":
            return module.verify_search(run, a.backend_root)
    elif task == "rules":
        if phase == "vectorize":
            module.check_evidence(run)
        elif phase == "apply":
            return module.apply(run, a.after_receipt)
        elif phase == "publish":
            module.publish_artifacts(run, read(run / "plan.json"), a.after_receipt)
            return receipt(run, status="published_pending_search", artifacts_published=True, search_verified=False)
        elif phase == "verify":
            return module.verify_search(run, a.backend_root)
    elif task == "excel":
        if phase == "load":
            return module.load(run, a.workers, a.chunk_batch_size)
        if phase == "finalize":
            return module.finalize(run, a.backend_root, a.probe_question, a.probe_preprocessed)
        if phase == "verify":
            from scripts.db import connect_postgres
            with connect_postgres() as conn:
                conn.execute("set transaction isolation level repeatable read read only")
                conn.execute("set local statement_timeout='600s'")
                return module.verify_entries(conn, read(run / "resume_inventory.json")["entries"])
    elif task == "publish":
        if phase == "prepare":
            return module.prepare(run, a.source_run, a.kind, a.after_receipt)
        if phase == "vectorize":
            _, docs = module.checked(run)
            if not docs:
                raise ValueError("No eligible reviewed recovery documents were selected")
        elif phase == "apply":
            return module.apply(run, a.after_receipt)
        elif phase == "publish":
            module.verify_database(run)
            plan, _ = module.checked(run)
            module.publish_artifacts(run, plan, a.after_receipt, mark_complete=False)
            return receipt(run, status="published_pending_search", artifacts_published=True)
        elif phase == "verify":
            return module.verify_search(run, a.backend_root)
    elif task == "sources":
        if phase == "scan":
            return module.scan(run, a.expected_db_only, a.expected_groups)
        if phase == "baseline":
            return module.search_cases(run, after=False, backend_root=a.backend_root)
        if phase == "apply":
            return module.apply(run, a.after_receipt)
        if phase == "verify":
            return module.verify(run, a.backend_root)
    return getattr(module, phase)(run)


def execute(task, run, phases, options):
    spec = TASKS[task]
    with ExitStack() as locks:
        # Also honor legacy locks so existing processes cannot overlap this CLI.
        if task == "missing":
            locks.enter_context(recovery_lock(run / ".pipeline.lock"))
        locks.enter_context(recovery_lock(run / spec.lock))
        validate_run(task, run, phases)
        atomic(run / "recovery_job.json", {"task": task, "version": 1})
        if task == "missing" and phases == ["resume"]:
            phases = missing_remaining(run)
        current = "import"
        try:
            module = importlib.import_module(spec.module) if phases else None
            for current in phases:
                event("phase_started", task=task, phase=current)
                dispatch(task, current, module, run, options)
                state = read(run / "receipt.json") if (run / "receipt.json").exists() else {}
                if state.get("status") == "waiting_for_ingestion":
                    event("sequence_paused", task=task, phase=current, status=state["status"])
                    return 2
            event("sequence_finished", task=task, phases=phases)
        except BaseException as error:
            if current == "search" and (run / "search_verification.json").exists():
                result = read(run / "search_verification.json")
                if result.get("status") == "running":
                    atomic(run / "search_verification.json", {**result, "status": "failed", "error": str(error)})
            receipt(run, status="interrupted" if isinstance(error, KeyboardInterrupt) else "needs_attention",
                    failed_phase=current, error=f"{type(error).__name__}: {error}",
                    database_verified=False, search_verified=False)
            raise
    return 0


def main(argv=None):
    p = parser()
    explicit = vars(p.parse_args(argv))
    if explicit.get("list"):
        event("tasks", tasks={k: [*v.phases, "status"] for k, v in TASKS.items()})
        return 0
    if "task" not in explicit or "run" not in explicit:
        p.error("--task and --run are required (or use --list)")
    task = explicit["task"]
    phases = explicit.get("phase", ["status"])
    try:
        if any(phase not in (*TASKS[task].phases, "status") for phase in phases):
            raise ValueError(f"Invalid phase for {task}; use --list")
        if len(set(phases)) != len(phases) or (len(phases) > 1 and {"status", "resume"}.intersection(phases)):
            raise ValueError("Do not repeat phases or combine status/resume with other phases")
        if len(phases) > 1:
            ordered = [TASKS[task].phases.index(phase) for phase in phases]
            if ordered != sorted(ordered):
                raise ValueError("Phases must follow the task order shown by --list")
        options = options_for(task, explicit)
        if task == "publish" and "prepare" in phases and (not options.source_run or not options.kind):
            raise ValueError("publish prepare requires --source-run and --kind")
        run = run_path(explicit["run"])
        validate_run(task, run, phases)
    except ValueError as error:
        p.error(str(error))
    if explicit.get("plan"):
        try:
            remaining = missing_remaining(run) if task == "missing" and phases == ["resume"] else phases
        except ValueError as error:
            p.error(str(error))
        event("plan", task=task, run=str(run), phases=remaining, executes=False)
        return 0
    if phases == ["status"]:
        state = read(run / "receipt.json") if (run / "receipt.json").exists() else {"status": "not_started"}
        keys = ("status", "updated_at", "failed_phase", "error", "database_verified", "search_verified",
                "selected_sources", "selected_members", "deferred_sources", "summary", "waiting_for")
        checkpoint = {}
        progress_file = {"excel": "db_load.json", "missing": "load.json"}.get(task)
        if progress_file and (run / progress_file).exists():
            progress = read(run / progress_file)
            checkpoint = {"status": progress.get("status"),
                          "completed_sources": len(progress.get("completed_sources", progress.get("completed", []))),
                          "concurrently_present_sources": len(progress.get("concurrently_present", [])),
                          "chunks_loaded": progress.get("chunks_loaded")}
        event("status", task=task, run=str(run), receipt={k: state[k] for k in keys if k in state}, checkpoint=checkpoint)
        return 0
    return execute(task, run, phases, options)


def legacy_main(task, argv=None, *, default_run=None, default_phase=None):
    """Keep existing module commands, but share all validation/locks/dispatch."""
    args = list(sys.argv[1:] if argv is None else argv)
    if "-h" in args or "--help" in args:
        return main(["--help"])
    prefix = ["--task", task]
    if default_run is not None and "--run" not in args and not any(a.startswith("--run=") for a in args):
        prefix += ["--run", str(default_run)]
    if args and not args[0].startswith("-"):
        prefix += ["--phase", args.pop(0)]
    elif default_phase:
        prefix += ["--phase", default_phase]
    return main(prefix + args)
