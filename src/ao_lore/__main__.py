"""Command-line entry point for standalone AO Lore evidence operations."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from ._strict_io import (
    ContractError,
    require_exact_keys,
    require_identifier,
    strict_read_json,
    write_exclusive_json,
)
from .candidate_queue import CandidateQueueError, list_candidates
from .candidates import CandidateError, append_review, inspect_candidate, persist_candidate
from .docling_pdf import create_docling_calibration_adapter
from .distillation import DistillationError
from .evaluation import compare_evaluation
from .home import repository_root, require_runtime_output, runtime_home
from .ingestion import (
    IngestionError,
    ingest_docx_document,
    ingest_single_document,
    ingest_verified_docx,
    read_verified_docx_source,
    resolve_docx_origin,
)
from .monitoring import evaluate_monitoring
from .parsing import ParsingError, validate_docling_benchmark
from .pdf_benchmark import LimitedPdfAdapter, load_pdf_corpus, run_pdf_benchmark


INGEST_INPUT_MAX_BYTES = 1024 * 1024
QUALIFIED_PDF_CORPUS_DIGEST = (
    "sha256:0a4a255050de73593319d61152470fba3c71bd7a82bf132272e05d6b77b66f5e"
)
INGEST_READBACK_KEYS = {
    "schema_version",
    "status",
    "candidate_id",
    "candidate_digest",
    "provenance_digest",
    "source_digest",
    "parser_id",
    "parser_version",
    "document_ir_digest",
    "parser_selection_report_digest",
    "parse_quality_report_digest",
    "distillation_trace_digest",
    "review_status",
    "next_commands",
    "canonical",
    "promotion_authority",
}
INGEST_DIGEST_FIELDS = (
    "candidate_digest",
    "provenance_digest",
    "source_digest",
    "document_ir_digest",
    "parser_selection_report_digest",
    "parse_quality_report_digest",
    "distillation_trace_digest",
)
def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ao-lore")
    surfaces = parser.add_subparsers(dest="surface", required=True)

    evaluation = surfaces.add_parser("evaluation", help="compare supplied evaluation attempts")
    evaluation_commands = evaluation.add_subparsers(dest="command", required=True)
    compare = evaluation_commands.add_parser("compare")
    compare.add_argument("--manifest", required=True)
    compare.add_argument("--out", required=True)

    monitoring = surfaces.add_parser("monitoring", help="evaluate supplied drift evidence")
    monitoring_commands = monitoring.add_subparsers(dest="command", required=True)
    evaluate = monitoring_commands.add_parser("evaluate")
    evaluate.add_argument("--observation", required=True)
    evaluate.add_argument("--baseline")
    evaluate.add_argument("--out", required=True)

    candidate = surfaces.add_parser("candidate", help="persist and review non-canonical candidates")
    candidate_commands = candidate.add_subparsers(dest="command", required=True)
    persist = candidate_commands.add_parser("persist")
    persist.add_argument("--result", required=True)
    persist.add_argument("--provenance", required=True)
    inspect = candidate_commands.add_parser("inspect")
    inspect.add_argument("--candidate-id", required=True)
    review = candidate_commands.add_parser("review")
    review.add_argument("--candidate-id", required=True)
    review.add_argument("--decision", choices=("accept", "reject"), required=True)
    review.add_argument("--reviewer", required=True)
    review.add_argument("--rationale", default="")
    listing = candidate_commands.add_parser("list")
    listing.add_argument(
        "--status",
        choices=("pending", "unreviewed", "accepted", "rejected", "all"),
        default="pending",
    )
    listing.add_argument("--limit", type=int, default=50)
    listing.add_argument("--after")
    listing.add_argument("--json", action="store_true")

    ingest = surfaces.add_parser(
        "ingest", help="ingest one born-digital PDF as a non-canonical candidate"
    )
    ingest.add_argument("--source", required=True)
    ingest.add_argument("--candidate-context")
    ingest.add_argument("--json", action="store_true")

    ingest_docx = surfaces.add_parser(
        "ingest-docx",
        help="ingest one qualified native DOCX as a non-canonical candidate",
    )
    ingest_docx.add_argument("--source", required=True)
    ingest_docx.add_argument("--candidate-context")
    ingest_docx.add_argument("--json", action="store_true")

    batch = surfaces.add_parser(
        "ingest-batch", help="ingest a bounded PDF manifest"
    )
    batch.add_argument("--manifest", required=True)
    batch.add_argument("--json", action="store_true")

    benchmark = surfaces.add_parser("benchmark", help="run local parser calibration")
    benchmark_commands = benchmark.add_subparsers(dest="command", required=True)
    pdf = benchmark_commands.add_parser("pdf")
    pdf.add_argument("--adapter", choices=("docling", "limited"), default="docling")
    pdf.add_argument("--corpus", required=True)
    pdf.add_argument("--out", required=True)
    docx = benchmark_commands.add_parser("docx")
    docx.add_argument("--out", required=True)

    promotion = surfaces.add_parser("promotion", help="operate governed local candidate promotion")
    promotion_commands = promotion.add_subparsers(dest="command", required=True)
    prepare = promotion_commands.add_parser("prepare")
    prepare.add_argument("--candidate-id", required=True)
    prepare.add_argument("--out", required=True)
    apply = promotion_commands.add_parser("apply")
    apply.add_argument("--proposal", required=True)
    apply.add_argument("--authorization", required=True)
    apply.add_argument("--json", action="store_true")
    promotion_inspect = promotion_commands.add_parser("inspect")
    promotion_inspect.add_argument("--promotion-id", required=True)
    promotion_inspect.add_argument("--json", action="store_true")
    rollback = promotion_commands.add_parser("rollback")
    rollback.add_argument("--promotion-id", required=True)
    rollback.add_argument("--authorization", required=True)
    rollback.add_argument("--json", action="store_true")
    recover = promotion_commands.add_parser("recover")
    recover.add_argument("--json", action="store_true")

    knowledge = surfaces.add_parser("knowledge", help="read committed canonical knowledge")
    knowledge_commands = knowledge.add_subparsers(dest="command", required=True)
    knowledge_status = knowledge_commands.add_parser("status")
    knowledge_status.add_argument("--json", action="store_true", required=True)
    knowledge_search = knowledge_commands.add_parser("search")
    knowledge_search.add_argument("--query", required=True)
    knowledge_search.add_argument("--limit", type=_knowledge_limit, default=20)
    knowledge_search.add_argument("--json", action="store_true", required=True)
    knowledge_answer = knowledge_commands.add_parser("answer")
    knowledge_answer.add_argument("--query", required=True)
    knowledge_answer.add_argument("--json", action="store_true", required=True)

    workspace = surfaces.add_parser(
        "workspace", help="operate one registered isolated workspace",
    )
    workspace_commands = workspace.add_subparsers(dest="command", required=True)
    workspace_list = workspace_commands.add_parser("list")
    workspace_list.add_argument("--json", action="store_true")
    workspace_ingest = workspace_commands.add_parser("ingest")
    workspace_ingest.add_argument("--workspace", type=_workspace_identifier, required=True)
    workspace_ingest.add_argument("--source", type=_workspace_source_locator, required=True)
    workspace_ingest.add_argument("--format", choices=("pdf", "docx"), required=True)
    workspace_ingest.add_argument(
        "--authority-role",
        choices=(
            "primary_law", "official_interpretation",
            "local_enforcement_guidance", "technical_guidance",
            "operator_procedure", "case_evidence",
        ),
        required=True,
    )
    workspace_ingest.add_argument(
        "--sensitivity", choices=("public", "internal", "restricted"), required=True,
    )
    workspace_ingest.add_argument("--json", action="store_true", required=True)
    for command in ("inspect", "refresh", "replay", "recover"):
        operation = workspace_commands.add_parser(command)
        operation.add_argument("--workspace", type=_workspace_identifier, required=True)
        operation.add_argument("--json", action="store_true")
    workspace_query = workspace_commands.add_parser("query")
    workspace_query.add_argument("--workspace", type=_workspace_identifier, required=True)
    workspace_query.add_argument("--prompt", type=_workspace_prompt, required=True)
    workspace_query.add_argument("--json", action="store_true")
    workspace_candidate = workspace_commands.add_parser("candidate")
    workspace_candidate_commands = workspace_candidate.add_subparsers(
        dest="candidate_command", required=True
    )
    candidate_prepare = workspace_candidate_commands.add_parser("prepare")
    candidate_prepare.add_argument("--workspace", type=_workspace_identifier, required=True)
    candidate_prepare.add_argument("--evidence", type=_selection_evidence_id, action="append", required=True)
    candidate_prepare.add_argument("--json", action="store_true", required=True)
    candidate_apply = workspace_candidate_commands.add_parser("apply")
    candidate_apply.add_argument("--workspace", type=_workspace_identifier, required=True)
    candidate_apply.add_argument("--proposal-id", type=_selection_identifier, required=True)
    candidate_apply.add_argument("--authorization-id", type=_selection_identifier, required=True)
    candidate_apply.add_argument("--json", action="store_true", required=True)
    candidate_inspect = workspace_candidate_commands.add_parser("inspect")
    candidate_inspect.add_argument("--workspace", type=_workspace_identifier, required=True)
    candidate_inspect.add_argument("--proposal-id", type=_selection_identifier, required=True)
    candidate_inspect.add_argument("--json", action="store_true", required=True)
    candidate_recover = workspace_candidate_commands.add_parser("recover")
    candidate_recover.add_argument("--workspace", type=_workspace_identifier, required=True)
    candidate_recover.add_argument("--json", action="store_true", required=True)
    return parser


def _knowledge_limit(value: str) -> int:
    if re.fullmatch(r"[0-9]+", value) is None:
        raise argparse.ArgumentTypeError("knowledge limit is invalid")
    limit = int(value, 10)
    if not 1 <= limit <= 200:
        raise argparse.ArgumentTypeError("knowledge limit is invalid")
    return limit


def _workspace_identifier(value: str) -> str:
    if re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", value) is None:
        raise argparse.ArgumentTypeError("workspace identifier is invalid")
    return value


def _workspace_prompt(value: str) -> str:
    if (
        not 1 <= len(value) <= 1024
        or any(unicodedata.category(character).startswith("C") for character in value)
    ):
        raise argparse.ArgumentTypeError("workspace prompt is invalid")
    return value


def _workspace_source_locator(value: str) -> str:
    if (
        re.fullmatch(r"inbox/[a-z0-9][a-z0-9._-]{0,127}", value) is None
        or not value.endswith((".pdf", ".docx"))
    ):
        raise argparse.ArgumentTypeError("workspace source locator is invalid")
    return value


def _selection_identifier(value: str) -> str:
    if re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", value) is None:
        raise argparse.ArgumentTypeError("selection identifier is invalid")
    return value


def _selection_evidence_id(value: str) -> str:
    if re.fullmatch(r"(?:sha256:[0-9a-f]{64}|[a-z0-9][a-z0-9._-]{0,127})", value) is None:
        raise argparse.ArgumentTypeError("selection evidence identifier is invalid")
    return value


def _promotion_dependencies():
    """Internal test seam; the public parser never accepts dependency roots."""

    return None


def _knowledge_dependencies():
    """Internal test seam; public knowledge commands retain fixed roots."""

    return None


def _run_knowledge(args: argparse.Namespace) -> dict:
    # Lazy import keeps unrelated AO Lore commands independent of the reader.
    from .knowledge import answer_knowledge, knowledge_status, search_knowledge
    from .knowledge_contracts import (
        validate_answer_readback, validate_search_readback, validate_status_readback,
    )

    dependencies = _knowledge_dependencies()
    options = {} if dependencies is None else {"_dependencies": dependencies}
    if args.command == "status":
        return validate_status_readback(knowledge_status(**options))
    if args.command == "search":
        return validate_search_readback(search_knowledge(args.query, limit=args.limit, **options))
    return validate_answer_readback(answer_knowledge(args.query, **options))


def _workspace_state_json(home: Path, workspace_id: str, relative: tuple[str, ...],
                          label: str) -> dict:
    value, _ = strict_read_json(
        home / "workspaces" / "state" / workspace_id / Path(*relative),
        label,
        max_bytes=4 * 1024 * 1024,
        root=home,
    )
    return value


def _workspace_graph(home: Path, definition: dict) -> dict:
    from .evidence_graph_contracts import validate_graph_manifest

    token = definition["graph_digest"].removeprefix("sha256:")
    value = _workspace_state_json(
        home, definition["workspace_id"], ("graph", "generations", token + ".json"),
        "workspace graph",
    )
    return validate_graph_manifest(value)


def _workspace_freshness(home: Path, definition: dict) -> dict | None:
    from .evidence_graph_contracts import (
        validate_detached_freshness_summary_against_manifest,
    )

    if definition["freshness_summary_status"] == "not_observed":
        return None
    value = _workspace_state_json(
        home, definition["workspace_id"],
        ("freshness", "summaries", definition["freshness_summary_id"] + ".json"),
        "workspace freshness summary",
    )
    return validate_detached_freshness_summary_against_manifest(
        value, _workspace_graph(home, definition),
    )


def _workspace_refresh_transport():
    """Return a separately reviewed private transport; absent in the clean core."""
    return None


def _workspace_refresh_adapter(dependencies, selection: object, graph: dict) -> dict:
    """Refresh through fixed retained policy material and a private transport."""

    from .evidence_acquisition import AcquisitionLimits, AcquisitionSpec
    from .evidence_graph_contracts import validate_freshness_policy
    from .workspace_runtime import open_workspace_context, refresh_workspace

    definition = selection.primary
    home = Path(dependencies.runtime_root)
    policy = validate_freshness_policy(_workspace_state_json(
        home, definition["workspace_id"],
        ("freshness", "policies", definition["freshness_policy_id"] + ".json"),
        "workspace freshness policy",
    ))
    specs = []
    for source in policy["sources"]:
        spec = AcquisitionSpec(
            source["source_id"], source["canonical_locator"],
            (source["prior_media_type"],),
            (),
        )
        object.__setattr__(spec, "successor_locator", source["declared_successor_locator"])
        object.__setattr__(spec, "successor_version", None)
        object.__setattr__(spec, "successor_effective_date", None)
        specs.append(spec)

    open_workspace_context(dependencies, definition["workspace_id"])
    transport = _workspace_refresh_transport()
    if transport is None or not callable(getattr(transport, "fetch", None)):
        raise ContractError("workspace refresh transport is unavailable")

    return refresh_workspace(
        dependencies, definition["workspace_id"], policy, graph, tuple(specs),
        http=transport,
        clock=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        monotonic=time.monotonic,
        limits=AcquisitionLimits(),
    )


def _dispatch_workspace(args: argparse.Namespace, dependencies=None) -> tuple[dict, dict]:
    from .workspace_query import (
        WorkspaceDocumentQuerySnapshot,
        WorkspaceGraphSnapshot,
        WorkspaceQuerySnapshot,
        query_workspace,
    )
    from .workspace_registry import (
        WorkspaceRegistryDependencies,
        inspect_workspace_registry,
        load_workspace_registry,
        select_workspace,
    )
    from .workspace_runtime import inspect_workspace, recover_workspace, replay_workspace

    home = runtime_home()
    if dependencies is None:
        dependencies = WorkspaceRegistryDependencies(home)
    snapshot = load_workspace_registry(dependencies)
    if args.command == "list":
        generation = None if not snapshot.generations else snapshot.generations[-1]
        return inspect_workspace_registry(snapshot), {
            "kind": "registry", "generation": generation,
        }

    selection = select_workspace(snapshot, args.workspace)
    generation = selection.generation
    selected = {selection.primary["workspace_id"], *(
        item["workspace_id"] for item in selection.references
    )}
    if args.command == "ingest":
        from .ingestion import ingest_workspace_document
        from .workspace_documents import (
            WorkspaceDocumentDependencies,
            load_workspace_documents,
        )

        report = ingest_workspace_document(
            dependencies,
            args.workspace,
            args.source,
            args.format,
            args.authority_role,
            args.sensitivity,
        )
        documents = load_workspace_documents(
            WorkspaceDocumentDependencies(Path(dependencies.runtime_root)),
            args.workspace,
        ).generation
        return report, {
            "kind": "document-ingest",
            "generation": documents,
            "selected": {args.workspace},
        }
    if args.command == "recover":
        report = recover_workspace(dependencies, args.workspace)
    elif args.command == "query":
        from .workspace_documents import (
            WorkspaceDocumentDependencies,
            load_workspace_documents,
        )

        graphs = []
        graph_values = []
        documents = []
        document_values = []
        for definition in (selection.primary, *selection.references):
            workspace_id = definition["workspace_id"]
            if definition.get("document_generation_digest") is not None:
                document = load_workspace_documents(
                    WorkspaceDocumentDependencies(Path(dependencies.runtime_root)),
                    workspace_id,
                ).generation
                if document is None:
                    raise ContractError("workspace document generation is unavailable")
                documents.append(WorkspaceDocumentQuerySnapshot(workspace_id, document))
                document_values.append(document)
            if definition.get("graph_id") is not None:
                graph = _workspace_graph(home, definition)
                graphs.append(WorkspaceGraphSnapshot(
                    workspace_id, graph, _workspace_freshness(home, definition),
                ))
                graph_values.append(graph)
        report = query_workspace(
            WorkspaceQuerySnapshot(
                snapshot, selection, tuple(graphs), documents=tuple(documents),
            ),
            args.workspace, args.prompt,
        )
        return report, {
            "kind": "query", "generation": generation, "selected": selected,
            "graphs": graph_values, "documents": document_values,
        }
    else:
        graph = _workspace_graph(home, selection.primary)
        freshness = _workspace_freshness(home, selection.primary)
        if args.command == "inspect":
            report = inspect_workspace(
                dependencies, args.workspace, graph,
                as_of_date=datetime.now(timezone.utc).date().isoformat(),
                freshness_summary=freshness,
            )
        elif args.command == "refresh":
            report = _workspace_refresh_adapter(dependencies, selection, graph)
        else:
            report = replay_workspace(
                dependencies, args.workspace, graph, freshness_summary=freshness,
            )
    return report, {
        "kind": "operation", "generation": generation, "selected": {args.workspace},
    }


def _workspace_query_snapshot(home: Path, dependencies, workspace_id: str):
    from .workspace_query import (
        WorkspaceDocumentQuerySnapshot,
        WorkspaceGraphSnapshot,
        WorkspaceQuerySnapshot,
    )
    from .workspace_registry import load_workspace_registry, select_workspace
    from .workspace_documents import WorkspaceDocumentDependencies, load_workspace_documents

    snapshot = load_workspace_registry(dependencies)
    selection = select_workspace(snapshot, workspace_id)
    graphs = []
    documents = []
    for definition in (selection.primary, *selection.references):
        current_id = definition["workspace_id"]
        if definition.get("document_generation_digest") is not None:
            document = load_workspace_documents(
                WorkspaceDocumentDependencies(Path(dependencies.runtime_root)),
                current_id,
            ).generation
            if document is None:
                raise ContractError("workspace document generation is unavailable")
            documents.append(WorkspaceDocumentQuerySnapshot(current_id, document))
        if definition.get("graph_id") is not None:
            graphs.append(
                WorkspaceGraphSnapshot(
                    current_id,
                    _workspace_graph(home, definition),
                    _workspace_freshness(home, definition),
                )
            )
    return WorkspaceQuerySnapshot(snapshot, selection, tuple(graphs), documents=tuple(documents))


def _run_workspace_candidate(args: argparse.Namespace) -> dict:
    from .evidence_selection import (
        EvidenceSelectionDependencies,
        apply_evidence_selection,
        inspect_evidence_selection,
        persist_prepared_selection,
        prepare_evidence_selection,
        recover_evidence_selections,
    )
    from .workspace_registry import WorkspaceRegistryDependencies

    home = runtime_home()
    dependencies = WorkspaceRegistryDependencies(home)
    selection_dependencies = EvidenceSelectionDependencies(
        runtime_root=home,
        candidate_root=repository_root() / "working" / "candidates",
        source_head=lambda: subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository_root(),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip(),
        now=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    )
    if args.candidate_command == "prepare":
        proposal = prepare_evidence_selection(
            _workspace_query_snapshot(home, dependencies, args.workspace),
            args.workspace,
            tuple(args.evidence),
            now=selection_dependencies.now(),
        )
        return persist_prepared_selection(selection_dependencies, proposal)
    if args.candidate_command == "apply":
        return apply_evidence_selection(
            selection_dependencies,
            args.proposal_id,
            args.authorization_id,
        )
    if args.candidate_command == "inspect":
        return inspect_evidence_selection(selection_dependencies, args.proposal_id)
    return recover_evidence_selections(selection_dependencies, args.workspace)


def _run_workspace(args: argparse.Namespace) -> dict:
    # Keep the complete workspace product boundary lazy for core help and every
    # unrelated AO Lore command.
    if args.command == "candidate":
        return _run_workspace_candidate(args)

    from .workspace_contracts import (
        validate_workspace_operation_readback,
        validate_workspace_query_readback,
        validate_workspace_registry_inspection,
    )
    from .document_evidence_contracts import (
        validate_workspace_document_ingest_readback,
    )

    report, context = _dispatch_workspace(args)
    kind = context.get("kind")
    if kind == "registry":
        return validate_workspace_registry_inspection(
            report, generation=context.get("generation"),
        )
    if kind == "operation":
        return validate_workspace_operation_readback(
            report, generation=context.get("generation"),
            selected_workspace_ids=context.get("selected"),
        )
    if kind == "document-ingest":
        generation = context.get("generation")
        if type(generation) is not dict:
            raise ContractError("workspace document generation is unavailable")
        return validate_workspace_document_ingest_readback(
            report,
            generation=generation,
            workspace_id=report.get("workspace_id"),
        )
    if kind == "query":
        return validate_workspace_query_readback(
            report, generation=context.get("generation"),
            selected_workspace_ids=context.get("selected"),
            graph_manifests=context.get("graphs"),
            document_generations=context.get("documents"),
        )
    raise ContractError("workspace readback context is invalid")


def _render_workspace(report: dict) -> str:
    if "workspace_ids" in report:
        lines = [f"STATUS\t{report['status']}", f"COUNT\t{report['workspace_count']}"]
        lines.extend(f"WORKSPACE\t{workspace_id}" for workspace_id in report["workspace_ids"])
        return "\n".join(lines) + "\n"
    workspace_id = report.get("workspace_id", report.get("primary_workspace_id"))
    status = report.get("status", report.get("outcome"))
    count = len(report.get("evidence", report.get("affected_workspace_ids", ())))
    reason = report["reason_code"]
    next_command = f"workspace inspect --workspace {workspace_id}"
    return (
        f"WORKSPACE\t{workspace_id}\nSTATUS\t{status}\nCOUNT\t{count}\n"
        f"RESULT\t{reason}\nNEXT\t{next_command}\n"
    )


def _validate_promotion_readback(value: object) -> dict:
    from .promotion import ID_RE, DIGEST_RE

    if type(value) is not dict:
        raise ContractError("promotion readback is invalid")
    required = {
        "schema_version", "operation", "status", "promotion_id", "proposal_id", "authorization_id",
        "transaction_id", "generation_id", "result_digest", "reason_code", "retry_safe", "next_command",
    }
    require_exact_keys(value, required, "promotion readback")
    if value["schema_version"] != "ao.lore.promotion-operation-readback.v0.1":
        raise ContractError("promotion readback is invalid")
    if value["operation"] not in {"prepare", "apply", "inspect", "rollback", "recover"}:
        raise ContractError("promotion readback is invalid")
    if value["status"] not in {"prepared", "committed", "inspected", "rolled_back", "recovered", "no_op", "rejected", "investigate"}:
        raise ContractError("promotion readback is invalid")
    for field in ("promotion_id", "proposal_id", "authorization_id", "transaction_id", "generation_id"):
        if value[field] is not None and (not isinstance(value[field], str) or ID_RE.fullmatch(value[field]) is None):
            raise ContractError("promotion readback is invalid")
    if value["result_digest"] is not None and (not isinstance(value["result_digest"], str) or DIGEST_RE.fullmatch(value["result_digest"]) is None):
        raise ContractError("promotion readback is invalid")
    if not isinstance(value["reason_code"], str) or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value["reason_code"]) is None:
        raise ContractError("promotion readback is invalid")
    if type(value["retry_safe"]) is not bool or value["next_command"] not in {"none", "promotion apply", "promotion inspect", "promotion rollback", "promotion recover"}:
        raise ContractError("promotion readback is invalid")
    return value


def _validate_promotion_inspection(value: object) -> dict:
    from .promotion import ID_RE, DIGEST_RE

    if type(value) is not dict:
        raise ContractError("promotion inspection is invalid")
    required = {
        "schema_version", "promotion_id", "status", "operation", "proposal_digest", "authorization_id",
        "authorization_digest", "transaction_id", "transaction_digest", "generation_id", "generation_manifest_digest",
        "recovery_classification", "audit_head_digest", "canonical_state_valid", "retry_safe", "next_command",
    }
    require_exact_keys(value, required, "promotion inspection")
    if value["schema_version"] != "ao.lore.promotion-inspection.v0.1":
        raise ContractError("promotion inspection is invalid")
    for field in ("promotion_id", "authorization_id", "transaction_id", "generation_id"):
        if value[field] is not None and (not isinstance(value[field], str) or ID_RE.fullmatch(value[field]) is None):
            raise ContractError("promotion inspection is invalid")
    for field in ("proposal_digest", "authorization_digest", "transaction_digest", "generation_manifest_digest", "audit_head_digest"):
        if value[field] is not None and (not isinstance(value[field], str) or DIGEST_RE.fullmatch(value[field]) is None):
            raise ContractError("promotion inspection is invalid")
    if type(value["canonical_state_valid"]) is not bool or type(value["retry_safe"]) is not bool:
        raise ContractError("promotion inspection is invalid")
    return value


def _run_promotion(args: argparse.Namespace) -> tuple[dict, bool]:
    # Lazy import keeps unrelated AO Lore commands independent of this surface.
    from .promotion import (
        apply_promotion, build_rollback_proposal, inspect_promotion,
        prepare_promotion, recover_promotions, rollback_promotion,
    )

    dependencies = _promotion_dependencies()
    options = {} if dependencies is None else {"dependencies": dependencies}
    if args.command == "prepare":
        proposal = prepare_promotion(args.candidate_id, Path(args.out), **options)
        report = {
            "schema_version": "ao.lore.promotion-operation-readback.v0.1", "operation": "prepare", "status": "prepared",
            "promotion_id": proposal["promotion_id"], "proposal_id": proposal["proposal_id"], "authorization_id": None,
            "transaction_id": None, "generation_id": proposal["expected_generation_id"], "result_digest": proposal["proposal_digest"],
            "reason_code": "none", "retry_safe": True, "next_command": "promotion apply",
        }
        return _validate_promotion_readback(report), False
    if args.command == "apply":
        return _validate_promotion_readback(apply_promotion(Path(args.proposal), Path(args.authorization), **options)), args.json
    if args.command == "inspect":
        return _validate_promotion_inspection(inspect_promotion(args.promotion_id, **options)), args.json
    if args.command == "rollback":
        build_rollback_proposal(args.promotion_id, **options)
        return _validate_promotion_readback(rollback_promotion(args.promotion_id, Path(args.authorization), **options)), args.json
    return _validate_promotion_readback(recover_promotions(**options)), args.json


def _render_promotion(report: dict) -> str:
    fields = ["status", "promotion_id", "transaction_id", "generation_id", "next_command"]
    return "".join(f"{field.upper()}\t{report[field]}\n" for field in fields if report.get(field) is not None)


def _candidate_input(path: str, label: str) -> dict:
    try:
        value, _ = strict_read_json(path, label, max_bytes=4 * 1024 * 1024)
    except (ContractError, OSError) as exc:
        raise CandidateError("candidate input is invalid") from exc
    return value


def _run_candidate(args: argparse.Namespace) -> dict:
    if args.command == "persist":
        return persist_candidate(
            _candidate_input(args.result, "distillation result"),
            _candidate_input(args.provenance, "candidate provenance"),
        )
    if args.command == "inspect":
        return inspect_candidate(args.candidate_id)
    if args.command == "list":
        status = "unreviewed" if args.status == "pending" else args.status
        return list_candidates(status=status, limit=args.limit, after=args.after)
    append_review(
        args.candidate_id,
        args.decision,
        args.reviewer,
        args.rationale,
    )
    return inspect_candidate(args.candidate_id)


def _escape_human_cell(value: str) -> str:
    escaped: list[str] = []
    for character in value:
        codepoint = ord(character)
        if character == "\\":
            escaped.append("\\\\")
        elif character == "\t":
            escaped.append("\\t")
        elif character == "\n":
            escaped.append("\\n")
        elif character == "\r":
            escaped.append("\\r")
        elif unicodedata.category(character).startswith("C") or character in "\u2028\u2029":
            if codepoint <= 0xFF:
                escaped.append(f"\\x{codepoint:02x}")
            elif codepoint <= 0xFFFF:
                escaped.append(f"\\u{codepoint:04x}")
            else:
                escaped.append(f"\\U{codepoint:08x}")
        else:
            escaped.append(character)
    return "".join(escaped)


def _print_candidate_queue(report: dict) -> None:
    print("CANDIDATE_ID\tREVIEW_STATUS\tCREATED_AT\tPARSER\tREVIEWS")
    for item in report["items"]:
        candidate_id = _escape_human_cell(item["candidate_id"])
        review_status = _escape_human_cell(item["review_status"])
        created_at = _escape_human_cell(item["created_at"])
        parser_id = _escape_human_cell(item["parser_id"])
        parser_version = _escape_human_cell(item["parser_version"])
        print(
            f"{candidate_id}\t{review_status}\t{created_at}\t"
            f"{parser_id}@{parser_version}\t{item['verified_review_events']}"
        )


def _ingest_profiles(manifest: dict) -> tuple[dict, dict]:
    selection_profile = {
        "profile_id": "pdf-docling-v1",
        "weights": {
            "structural_fidelity": 0.4,
            "text_fidelity": 0.4,
            "source_location_fidelity": 0.2,
        },
        "penalty_weights": {},
        "normalization_bounds": {},
        "missing_optional_policy": "ineligible",
        "tie_break_order": [
            "required_feature_coverage",
            "structural_fidelity",
            "parser_id",
        ],
        "fixture_corpus_digest": manifest.get("fixture_corpus_digest"),
    }
    quality_profile = {
        "profile_id": "pdf-quality-v1",
        "component_weights": {
            "text_coverage": 0.4,
            "structural_completeness": 0.3,
            "source_span_coverage": 0.2,
            "document_ir_valid": 0.1,
        },
        "accept_threshold": 0.8,
        "fallback_threshold": 0.7,
        "maximum_fallbacks": 0,
        "critical_failures": [
            "invalid_ir",
            "source_digest_mismatch",
            "parser_crash",
            "no_readable_content",
            "lost_provenance",
        ],
        "calibration_result_digest": manifest.get("result_digest"),
        "intermediate_decision": "quarantine",
        "exhausted_decision": "reject",
    }
    return selection_profile, quality_profile


def _docx_ingest_profiles(manifest: dict) -> tuple[dict, dict]:
    selection_profile = {
        "profile_id": "docx-native-v1",
        "weights": {
            "structural_fidelity": 0.5,
            "text_fidelity": 0.5,
        },
        "penalty_weights": {},
        "normalization_bounds": {},
        "missing_optional_policy": "ineligible",
        "tie_break_order": [
            "required_feature_coverage",
            "structural_fidelity",
            "parser_id",
        ],
        "fixture_corpus_digest": manifest.get("fixture_corpus_digest"),
    }
    quality_profile = {
        "profile_id": "docx-quality-v1",
        "component_weights": {
            "text_coverage": 0.5,
            "structural_completeness": 0.3,
            "document_ir_valid": 0.2,
        },
        "accept_threshold": 0.8,
        "fallback_threshold": 0.0,
        "maximum_fallbacks": 0,
        "critical_failures": [
            "invalid_ir",
            "source_digest_mismatch",
            "parser_crash",
            "no_readable_content",
            "lost_provenance",
        ],
        "calibration_result_digest": manifest.get("result_digest"),
        "intermediate_decision": "reject",
        "exhausted_decision": "reject",
    }
    return selection_profile, quality_profile


def _run_ingest(args: argparse.Namespace) -> dict:
    home = runtime_home()
    manifest, _ = strict_read_json(
        home / "benchmarks" / "docling-2.118.1.json",
        "Docling qualification manifest",
        max_bytes=INGEST_INPUT_MAX_BYTES,
        root=home,
    )
    context = None
    if args.candidate_context is not None:
        context, _ = strict_read_json(
            args.candidate_context,
            "candidate context",
            max_bytes=INGEST_INPUT_MAX_BYTES,
            root=repository_root() / "working",
        )
    selection_profile, quality_profile = _ingest_profiles(manifest)
    return ingest_single_document(
        args.source,
        benchmark_manifest=manifest,
        selection_profile=selection_profile,
        quality_profile=quality_profile,
        candidate_context=context,
    )


def _run_ingest_docx(args: argparse.Namespace) -> dict:
    from .docx_benchmark import fixed_docx_benchmark_paths
    from .private_docx_domain import validate_docx_expectation

    home = runtime_home()
    paths = fixed_docx_benchmark_paths(home)
    expectation, _ = strict_read_json(
        paths["expectation"],
        "private DOCX expectation",
        max_bytes=8 * 1024 * 1024,
        root=home,
    )
    validated_expectation = validate_docx_expectation(expectation)
    manifest, _ = strict_read_json(
        paths["qualification"],
        "DOCX qualification manifest",
        max_bytes=INGEST_INPUT_MAX_BYTES,
        root=home,
    )
    manifest = _validate_docx_qualification(
        manifest, expected_corpus_digest=validated_expectation.corpus_digest
    )
    context = None
    if args.candidate_context is not None:
        context, _ = strict_read_json(
            args.candidate_context,
            "candidate context",
            max_bytes=INGEST_INPUT_MAX_BYTES,
            root=repository_root() / "working",
        )
    data, origin, expected_corpus_digest = resolve_docx_origin(
        args.source, expectation=validated_expectation
    )
    digest = origin.derived_digest
    selection_profile, quality_profile = _docx_ingest_profiles(manifest)
    return ingest_verified_docx(
        data,
        source_digest=digest,
        resource="source-" + digest.removeprefix("sha256:")[:16] + ".docx",
        benchmark_manifest=manifest,
        expected_corpus_digest=expected_corpus_digest,
        selection_profile=selection_profile,
        quality_profile=quality_profile,
        origin=origin,
        candidate_context=context,
    )


def _validate_ingest_readback(
    value: object, *, expected_parser_id: str, expected_parser_version: str
) -> dict:
    if type(value) is not dict:
        raise ContractError("ingest readback must be an object")
    report = value
    require_exact_keys(report, INGEST_READBACK_KEYS, "ingest readback")
    if report["schema_version"] != "ao.lore.single-document-ingest-readback.v0.1":
        raise ContractError("ingest readback schema version is unsupported")
    if report["status"] not in ("created", "unchanged"):
        raise ContractError("ingest readback status is invalid")
    candidate_id = require_identifier(report["candidate_id"], "candidate_id")
    if not candidate_id.startswith("candidate-"):
        raise ContractError("candidate_id must use the candidate- prefix")
    for field in INGEST_DIGEST_FIELDS:
        digest = report[field]
        if not isinstance(digest, str) or re.fullmatch(
            r"sha256:[0-9a-f]{64}", digest
        ) is None:
            raise ContractError(f"ingest readback {field} is invalid")
    parser_id = require_identifier(report["parser_id"], "parser_id")
    if (
        parser_id != expected_parser_id
        or report["parser_version"] != expected_parser_version
    ):
        raise ContractError("ingest readback parser identity is unqualified")
    if report["review_status"] not in ("unreviewed", "accepted", "rejected"):
        raise ContractError("ingest readback review status is invalid")
    commands = report["next_commands"]
    if (
        not isinstance(commands, list)
        or not 1 <= len(commands) <= 8
        or any(
            not isinstance(command, str) or not 1 <= len(command) <= 1024
            for command in commands
        )
    ):
        raise ContractError("ingest readback next commands are invalid")
    expected_commands = [
        f"ao-lore candidate inspect --candidate-id {candidate_id}",
        f"ao-lore candidate review --candidate-id {candidate_id} "
        "--decision accept --reviewer <reviewer-id>",
    ]
    if commands != expected_commands:
        raise ContractError("ingest readback next commands do not match candidate")
    if (
        report["canonical"] is not False
        or report["promotion_authority"] is not False
    ):
        raise ContractError("ingest readback cannot claim authority")
    return report


def _validate_docx_qualification(value: object, *, expected_corpus_digest: str) -> dict:
    from .docx_ooxml import (
        DOCX_PARSER_ID,
        DOCX_PARSER_VERSION,
        DocxLimits,
        docx_configuration_digest,
        validate_docx_benchmark,
    )

    if not isinstance(value, dict):
        raise ContractError("DOCX qualification manifest must be an object")
    validate_docx_benchmark(
        value,
        expected_configuration_digest=docx_configuration_digest(DocxLimits()),
        expected_corpus_digest=expected_corpus_digest,
    )
    if (
        value.get("parser_id") != DOCX_PARSER_ID
        or value.get("parser_version") != DOCX_PARSER_VERSION
        or value.get("decision") != "hold"
    ):
        raise ParsingError("DOCX benchmark evidence is invalid")
    return value


def _render_ingest(report: dict) -> str:
    lines = [
        f"STATUS\t{_escape_human_cell(report['status'])}",
        f"CANDIDATE_ID\t{_escape_human_cell(report['candidate_id'])}",
        f"REVIEW_STATUS\t{_escape_human_cell(report['review_status'])}",
    ]
    for command in report["next_commands"]:
        lines.append(f"NEXT\t{_escape_human_cell(command)}")
    return "\n".join(lines) + "\n"


def _run_ingest_batch(args: argparse.Namespace) -> dict:
    """Load an exact batch manifest, route qualification, and delegate lazily."""

    from .batch_ingestion import (
        BatchDependencies,
        _docx_signature_matches,
        ingest_verified_batch,
        load_batch_manifest,
    )
    from .private_docx_domain import DOCX_MIME, validate_docx_expectation

    locator = args.manifest
    if (
        not isinstance(locator, str)
        or not locator
        or "\\" in locator
        or locator.startswith("/")
        or any(part in {"", ".", ".."} for part in locator.split("/"))
    ):
        raise ContractError("batch manifest locator is invalid")
    manifest, manifest_digest = load_batch_manifest(args.manifest)
    home = runtime_home()
    if manifest["schema_version"] == "ao.lore.ingest-batch-manifest.v0.1":
        qualification, _ = strict_read_json(
            home / "benchmarks" / "docling-2.118.1.json",
            "Docling qualification manifest",
            max_bytes=INGEST_INPUT_MAX_BYTES,
            root=home,
        )
        qualification = _validate_batch_qualification(qualification)
        selection_profile, quality_profile = _ingest_profiles(qualification)
        dependencies = BatchDependencies(
            benchmark_manifest=qualification,
            selection_profile=selection_profile,
            quality_profile=quality_profile,
        )
    elif manifest["schema_version"] == "ao.lore.ingest-batch-manifest.v0.2":
        from .docx_benchmark import fixed_docx_benchmark_paths

        paths = fixed_docx_benchmark_paths(home)
        expectation, _ = strict_read_json(
            paths["expectation"],
            "private DOCX expectation",
            max_bytes=8 * 1024 * 1024,
            root=home,
        )
        validated_expectation = validate_docx_expectation(expectation)
        qualification, _ = strict_read_json(
            paths["qualification"],
            "DOCX qualification manifest",
            max_bytes=INGEST_INPUT_MAX_BYTES,
            root=home,
        )
        qualification = _validate_docx_qualification(
            qualification, expected_corpus_digest=validated_expectation.corpus_digest
        )
        selection_profile, quality_profile = _docx_ingest_profiles(qualification)

        def origin_for(source_path: str | Path, source_bytes: bytes):
            if type(source_bytes) is not bytes:
                raise IngestionError("verified document binding is invalid")
            data, origin, corpus_digest = resolve_docx_origin(
                source_path, expectation=validated_expectation
            )
            if (
                data != source_bytes
                or corpus_digest != validated_expectation.corpus_digest
            ):
                raise IngestionError("verified document binding is invalid")
            return origin

        dependencies = BatchDependencies(
            benchmark_manifest=qualification,
            selection_profile=selection_profile,
            quality_profile=quality_profile,
            format_id="docx",
            media_type=DOCX_MIME,
            extension=".docx",
            parser_id="native-docx-ooxml",
            parser_version="1.0.0",
            signature_check=_docx_signature_matches,
            expected_corpus_digest=validated_expectation.corpus_digest,
            origin_for=origin_for,
            ingest_one=ingest_docx_document,
            read_source=read_verified_docx_source,
        )
    else:
        raise ContractError("batch manifest schema version is unsupported")
    return ingest_verified_batch(
        manifest,
        manifest_digest,
        dependencies=dependencies,
    )


def _validate_batch_qualification(value: object) -> dict:
    """Require the exact production Docling identity and configuration."""

    if not isinstance(value, dict):
        raise ContractError("Docling qualification manifest must be an object")
    from .docling_pdf import configuration_digest

    validate_docling_benchmark(
        value,
        expected_configuration_digest=configuration_digest(
            50 * 1024 * 1024, 500, 100_000
        ),
    )
    if value.get("fixture_corpus_digest") != QUALIFIED_PDF_CORPUS_DIGEST:
        raise ParsingError("Docling benchmark evidence has an unqualified corpus")
    return value


def _validate_batch_readback(value: object) -> dict:
    from .batch_ingestion import validate_final_batch_readback

    if not isinstance(value, dict):
        raise ContractError("batch ingest readback must be an object")
    return validate_final_batch_readback(value)


def _render_batch(report: dict) -> str:
    lines = [
        f"STATUS\t{_escape_human_cell(report['status'])}",
        f"BATCH_ID\t{_escape_human_cell(report['batch_id'])}",
        f"COUNTS\tcreated={report['created']}\tunchanged={report['unchanged']}"
        f"\trejected={report['rejected']}\ttotal={report['total']}",
    ]
    for item in report["items"]:
        detail = (
            item["candidate_id"]
            if item["status"] in {"created", "unchanged"}
            else item["error_code"]
        )
        lines.append(
            f"ITEM\t{_escape_human_cell(item['item_id'])}"
            f"\t{_escape_human_cell(item['status'])}"
            f"\t{_escape_human_cell(detail)}"
        )
    for command in report["next_commands"]:
        lines.append(f"NEXT\t{_escape_human_cell(command)}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.surface == "workspace":
        try:
            report = _run_workspace(args)
            rendered = (
                json.dumps(report, sort_keys=True) + "\n"
                if args.json else _render_workspace(report)
            )
            sys.stdout.write(rendered)
        except Exception:
            sys.stderr.write("ao-lore: workspace operation rejected\n")
            return 2
        return 0
    if args.surface == "knowledge":
        try:
            report = _run_knowledge(args)
            rendered = json.dumps(
                report, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"),
            ) + "\n"
            sys.stdout.write(rendered)
        except Exception:
            # Query text, paths, parser details, and exception text never cross
            # this one content-free knowledge rejection boundary.
            sys.stderr.write("ao-lore: knowledge operation rejected\n")
            return 2
        return 0
    if args.surface == "promotion":
        try:
            report, as_json = _run_promotion(args)
            rendered = json.dumps(report, sort_keys=True) + "\n" if as_json else _render_promotion(report)
            sys.stdout.write(rendered)
        except (ContractError, OSError, TypeError, ValueError):
            # PromotionError is a ValueError. Exception text and input paths
            # never cross this single public redaction boundary.
            sys.stderr.write("ao-lore: promotion operation rejected\n")
            return 2
        return 0
    if args.surface in {"ingest", "ingest-docx"}:
        try:
            if args.surface == "ingest":
                report = _validate_ingest_readback(
                    _run_ingest(args),
                    expected_parser_id="docling",
                    expected_parser_version="2.118.1",
                )
            else:
                report = _validate_ingest_readback(
                    _run_ingest_docx(args),
                    expected_parser_id="native-docx-ooxml",
                    expected_parser_version="1.0.0",
                )
            rendered = (
                json.dumps(report, sort_keys=True) + "\n"
                if args.json
                else _render_ingest(report)
            )
            sys.stdout.write(rendered)
        except (
            IngestionError,
            ParsingError,
            DistillationError,
            CandidateError,
            ContractError,
            OSError,
            TypeError,
            ValueError,
        ):
            print("ao-lore: document ingest rejected", file=sys.stderr)
            return 2
        return 0
    if args.surface == "ingest-batch":
        try:
            report = _validate_batch_readback(_run_ingest_batch(args))
            rendered = (
                json.dumps(report, sort_keys=True) + "\n"
                if args.json
                else _render_batch(report)
            )
            sys.stdout.write(rendered)
        except Exception:
            print("ao-lore: batch ingest rejected", file=sys.stderr)
            return 2
        return 0
    try:
        if args.surface == "candidate":
            report = _run_candidate(args)
            if args.command == "list" and not args.json:
                _print_candidate_queue(report)
            else:
                print(json.dumps(report, sort_keys=True))
            return 0
        if args.surface == "benchmark":
            output = require_runtime_output(args.out)
            if not output.parent.exists():
                raise ParsingError("benchmark output parent does not exist")
            if args.command == "docx":
                from .docx_benchmark import (
                    fixed_docx_benchmark_paths,
                    load_fixed_docx_fixture_data,
                    run_docx_benchmark,
                )

                from .docx_ooxml import NativeDocxOoxmlAdapter

                paths = fixed_docx_benchmark_paths(runtime_home())
                expectation, _ = strict_read_json(
                    paths["expectation"], "private DOCX expectation", max_bytes=8 * 1024 * 1024
                )
                qualification, _ = strict_read_json(
                    paths["qualification"], "DOCX qualification manifest", max_bytes=2 * 1024 * 1024
                )
                fixture_data = load_fixed_docx_fixture_data(paths["corpus"])
                adapter = NativeDocxOoxmlAdapter(
                    qualification,
                    expected_corpus_digest=qualification["fixture_corpus_digest"],
                )
                report = run_docx_benchmark(adapter, expectation, fixture_data=fixture_data)
            else:
                corpus = load_pdf_corpus(args.corpus)
                config = corpus["configuration"]
                if args.adapter == "docling":
                    adapter = create_docling_calibration_adapter(
                        max_file_size=config["max_file_size"],
                        max_num_pages=config["max_num_pages"],
                        max_blocks=config["max_blocks"],
                    )
                else:
                    adapter = LimitedPdfAdapter(max_file_size=config["max_file_size"])
                report = run_pdf_benchmark(
                    adapter,
                    corpus,
                    fixture_root=Path(args.corpus).parent,
                    runtime=f"python-{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
                    platform=f"{platform.system().lower()}-{platform.machine().lower()}",
                )
            try:
                write_exclusive_json(output, report, root=runtime_home())
            except (ContractError, OSError) as exc:
                raise ParsingError("benchmark output was rejected") from exc
            print(json.dumps({"status": "written", "result_digest": report["result_digest"]}, sort_keys=True))
            return 0
        output = require_runtime_output(args.out)
        if not output.parent.exists():
            raise ContractError(f"output parent does not exist: {output.parent}")
        if args.surface == "evaluation":
            report = compare_evaluation(
                args.manifest,
                output_path=output,
                output_root=runtime_home(),
            )
        else:
            report = evaluate_monitoring(
                args.observation,
                baseline_path=args.baseline,
                output_path=output,
                output_root=runtime_home(),
            )
    except CandidateQueueError:
        print("ao-lore: candidate operation rejected", file=sys.stderr)
        return 2
    except CandidateError:
        print("ao-lore: candidate operation rejected", file=sys.stderr)
        return 2
    except ParsingError:
        print("ao-lore: benchmark rejected", file=sys.stderr)
        return 2
    except (ContractError, OSError) as exc:
        print(f"ao-lore: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"status": "written", "output": str(Path(args.out)), "result": report.get("result", report.get("status"))}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
