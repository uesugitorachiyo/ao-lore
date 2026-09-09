import inspect
import json
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ao_lore.__main__ import _dispatch_workspace, _parser, main
from ao_lore.benchmark import canonical_digest
from ao_lore.workspace_contracts import AUTHORITY_FIELDS
from ao_lore.workspace_documents import (
    WorkspaceDocumentDependencies,
    publish_workspace_documents,
)
from ao_lore.workspace_registry import (
    WorkspaceRegistryDependencies,
    publish_workspace_registry_generation,
)

HTTPS = "https" + "://"
TEST_TLD = "." + "test"


class WorkspaceCliTests(unittest.TestCase):
    def invoke(self, argv):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_parser_exposes_only_the_fixed_workspace_grammar(self):
        parser = _parser()
        accepted = (
            ["workspace", "list", "--json"],
            ["workspace", "ingest", "--workspace", "primary-fixture-a",
             "--source", "inbox/manual.pdf", "--format", "pdf",
             "--authority-role", "operator_procedure", "--sensitivity", "internal",
             "--json"],
            ["workspace", "inspect", "--workspace", "primary-fixture-a", "--json"],
            ["workspace", "query", "--workspace", "primary-fixture-a", "--prompt", "safe", "--json"],
            ["workspace", "refresh", "--workspace", "shared-reference-fixture", "--json"],
            ["workspace", "replay", "--workspace", "primary-fixture-a", "--json"],
            ["workspace", "recover", "--workspace", "primary-fixture-a", "--json"],
        )
        for argv in accepted:
            with self.subTest(argv=argv):
                parser.parse_args(argv)

        for command in ("inspect", "query", "refresh", "replay", "recover"):
            argv = ["workspace", command]
            if command == "query":
                argv += ["--prompt", "safe"]
            with self.subTest(command=command), self.assertRaises(SystemExit):
                parser.parse_args(argv)
        with self.assertRaises(SystemExit):
            parser.parse_args(["workspace", "unknown"])

    def test_workspace_ingest_grammar_is_closed_and_requires_json(self):
        parser = _parser()
        base = [
            "workspace", "ingest", "--workspace", "primary-fixture-a",
            "--source", "inbox/manual.pdf", "--format", "pdf",
            "--authority-role", "operator_procedure", "--sensitivity", "internal",
        ]
        with self.assertRaises(SystemExit):
            parser.parse_args(base)
        for index in (7, 9, 11):
            changed = [*base, "--json"]
            changed[index] = {7: "txt", 9: "administrator", 11: "secret"}[index]
            with self.subTest(index=index), self.assertRaises(SystemExit):
                parser.parse_args(changed)

    def test_workspace_ingest_failure_is_exactly_redacted(self):
        argv = [
            "workspace", "ingest", "--workspace", "primary-fixture-a",
            "--source", "inbox/customer-secret.pdf", "--format", "pdf",
            "--authority-role", "operator_procedure", "--sensitivity", "internal",
            "--json",
        ]
        with patch(
            "ao_lore.__main__._dispatch_workspace",
            side_effect=ValueError(str(Path(
                "/", "opt", "fixture-private", "customer-secret.pdf",
            ))),
        ):
            code, out, err = self.invoke(argv)
        self.assertEqual((2, "", "ao-lore: workspace operation rejected\n"), (code, out, err))

    def test_workspace_identifier_and_prompt_are_bounded_at_parse_time(self):
        parser = _parser()
        invalid_ids = ("Upper", "-leading", "x" * 129, "private/path", "")
        for workspace_id in invalid_ids:
            with self.subTest(workspace_id=workspace_id), self.assertRaises(SystemExit):
                parser.parse_args([
                    "workspace", "inspect", "--workspace", workspace_id,
                ])
        for prompt in (
            "", "x" * 1025, "unsafe\ntext", "unsafe\x7ftext",
            "unsafe\x85text", "unsafe\u202etext",
        ):
            with self.subTest(prompt=prompt), self.assertRaises(SystemExit):
                parser.parse_args([
                    "workspace", "query", "--workspace", "primary-fixture-a",
                    "--prompt", prompt,
                ])
        parsed = parser.parse_args([
            "workspace", "query", "--workspace", "primary-fixture-a",
            "--prompt", "¿Qué evidencia hay?",
        ])
        self.assertEqual("¿Qué evidencia hay?", parsed.prompt)

    def test_invalid_prompt_error_does_not_echo_prompt(self):
        parser = _parser()
        prompt = "customer-secret\u202ehidden"
        stderr = StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit):
            parser.parse_args([
                "workspace", "query", "--workspace", "primary-fixture-a",
                "--prompt", prompt,
            ])
        self.assertNotIn("customer-secret", stderr.getvalue())
        self.assertIn("workspace prompt is invalid", stderr.getvalue())

    def test_workspace_surface_rejects_every_forbidden_control(self):
        parser = _parser()
        forbidden = (
            "--root", "--url", "--manifest", "--destination", "--global",
            "--global-search", "--recursive", "--recursive-reference", "--source-head",
            "--clock", "--timeout", "--budget", "--policy", "--trust", "--provider",
            "--failpoint", "--overwrite", "--force", "--concurrency", "--authority",
            "--loader", "--module", "--host", "--resolver", "--peer", "--transport",
            "--path",
        )
        switches = {"--global", "--global-search", "--recursive", "--recursive-reference",
                    "--overwrite", "--force"}
        for flag in forbidden:
            argv = ["workspace", "refresh", "--workspace", "primary-fixture-a", flag]
            if flag not in switches:
                argv.append("x")
            with self.subTest(flag=flag), self.assertRaises(SystemExit):
                parser.parse_args(argv)

    def test_workspace_query_rejects_graph_and_runtime_overrides(self):
        parser = _parser()
        for flag in (
            "--graph-required", "--root", "--manifest", "--url", "--provider",
            "--model", "--network", "--policy", "--force", "--authority",
        ):
            argv = [
                "workspace", "query", "--workspace", "workspace-a",
                "--prompt", "procedure record", "--json", flag,
            ]
            if flag not in {"--graph-required", "--network", "--force"}:
                argv.append("x")
            with self.subTest(flag=flag), self.assertRaises(SystemExit):
                parser.parse_args(argv)

    def test_workspace_query_loads_documents_when_graph_is_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            definition = {
                "schema_version": "ao.lore.workspace-definition.v0.2",
                "workspace_id": "workspace-a", "workspace_version": 1,
                "workspace_type": "property", "domain": "synthetic-guidance",
                "jurisdiction": "fixture-scope", "lifecycle_status": "active",
                "root_workflow_id": "workflow-workspace-a",
                "root_workflow_digest": canonical_digest("workflow-workspace-a"),
                "source_registry_id": "sources-workspace-a",
                "source_registry_digest": canonical_digest("sources-workspace-a"),
                "graph_id": None, "graph_digest": None,
                "freshness_policy_id": None, "freshness_policy_digest": None,
                "freshness_summary_status": "not_observed",
                "freshness_summary_id": None, "freshness_summary_digest": None,
                "reference_workspace_ids": [],
                "document_store_id": "documents-workspace-a",
                "document_generation_digest": None,
                "definition_digest": canonical_digest("placeholder"),
                **{field: False for field in AUTHORITY_FIELDS},
            }
            definition["definition_digest"] = canonical_digest({
                key: value for key, value in definition.items()
                if key != "definition_digest"
            })
            publish_workspace_registry_generation(
                (definition,), WorkspaceRegistryDependencies(root),
            )
            document = {
                "schema_version": "ao.lore.document-ir.v0.1",
                "document_id": "procedure-manual",
                "source": {
                    "resource": "inbox/procedure-manual.pdf",
                    "digest": canonical_digest("synthetic-source"),
                    "media_type": "application/pdf",
                },
                "blocks": [{
                    "id": "block-1", "type": "paragraph",
                    "text": "Send a procedure record before scheduling the fixture visit.",
                    "source_span": {"page": 1, "start": 0, "end": 58},
                }],
                "metadata": {
                    "source_id": "source-procedure-manual",
                    "authority_role": "operator_procedure", "sensitivity": "internal",
                    "version": "1", "effective_date": "2026-08-13",
                    "qualification_codes": ["synthetic-only"],
                },
            }
            publish_workspace_documents(
                WorkspaceDocumentDependencies(root), "workspace-a", (document,),
            )
            with patch("ao_lore.__main__.runtime_home", return_value=root):
                code, out, err = self.invoke([
                    "workspace", "query", "--workspace", "workspace-a",
                    "--prompt", "procedure record", "--json",
                ])
            self.assertEqual((0, ""), (code, err))
            result = json.loads(out)
            self.assertEqual("answer", result["outcome"])
            self.assertEqual("document_block", result["evidence"][0]["evidence_kind"])

    def test_fresh_runtime_lists_empty_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "absent-runtime"
            before = set(Path(directory).iterdir())
            with patch("ao_lore.__main__.runtime_home", return_value=root):
                code, out, err = self.invoke(["workspace", "list", "--json"])
            self.assertEqual(0, code)
            self.assertEqual("", err)
            report = json.loads(out)
            self.assertEqual("empty", report["status"])
            self.assertEqual([], report["workspace_ids"])
            self.assertEqual(before, set(Path(directory).iterdir()))

    def test_dispatch_readback_is_revalidated_before_one_json_write(self):
        invalid = {"status": "active", "private_path": "/tmp/customer-one"}
        with patch("ao_lore.__main__._dispatch_workspace", return_value=(invalid, {})), patch(
            "sys.stdout.write"
        ) as write:
            code, out, err = self.invoke([
                "workspace", "inspect", "--workspace", "primary-fixture-a", "--json",
            ])
        self.assertEqual((2, "", "ao-lore: workspace operation rejected\n"), (code, out, err))
        write.assert_not_called()

    def test_ordinary_exceptions_are_exactly_redacted(self):
        messages = (
            str(Path(
                "/", "opt", "fixture-private", "customer-one", "file.json",
            )),
            "customer-one prompt: should I take action?",
            "primary-fixture-a secret source content",
        )
        for message in messages:
            with self.subTest(message=message), patch(
                "ao_lore.__main__._dispatch_workspace", side_effect=ValueError(message),
            ):
                code, out, err = self.invoke([
                    "workspace", "query", "--workspace", "primary-fixture-a",
                    "--prompt", "private prompt", "--json",
                ])
            self.assertEqual((2, "", "ao-lore: workspace operation rejected\n"), (code, out, err))

    def test_process_controls_propagate(self):
        argv = ["workspace", "recover", "--workspace", "primary-fixture-a"]
        for control in (KeyboardInterrupt(), SystemExit(17)):
            with self.subTest(control=type(control).__name__), patch(
                "ao_lore.__main__._dispatch_workspace", side_effect=control,
            ), self.assertRaises(type(control)) as raised:
                main(argv)
            if isinstance(control, SystemExit):
                self.assertEqual(17, raised.exception.code)

    def test_actual_offline_dispatch_constructs_no_network_client(self):
        __import__("ao_lore.workspace_query")
        __import__("ao_lore.workspace_registry")
        __import__("ao_lore.workspace_runtime")
        definition = {
            "workspace_id": "primary-fixture-a",
            "reference_workspace_ids": [],
        }
        generation = {"workspaces": [definition]}
        snapshot = SimpleNamespace(generations=(generation,))
        selection = SimpleNamespace(
            generation=generation, primary=definition, references=(),
        )
        graph = {"graph_id": "graph-fixture"}
        commands = (
            Namespace(command="list", json=True),
            Namespace(command="inspect", workspace="primary-fixture-a", json=True),
            Namespace(command="query", workspace="primary-fixture-a", prompt="safe", json=True),
            Namespace(command="replay", workspace="primary-fixture-a", json=True),
            Namespace(command="recover", workspace="primary-fixture-a", json=True),
        )
        with patch("ao_lore.__main__.runtime_home", return_value=Path("/fixed/runtime")), patch(
            "ao_lore.workspace_registry.load_workspace_registry", return_value=snapshot,
        ), patch(
            "ao_lore.workspace_registry.select_workspace", return_value=selection,
        ), patch(
            "ao_lore.workspace_registry.inspect_workspace_registry", return_value={"status": "empty"},
        ), patch(
            "ao_lore.__main__._workspace_graph", return_value=graph,
        ), patch(
            "ao_lore.__main__._workspace_freshness", return_value=None,
        ), patch(
            "ao_lore.workspace_runtime.inspect_workspace", return_value={"operation": "inspect"},
        ), patch(
            "ao_lore.workspace_runtime.replay_workspace", return_value={"operation": "replay"},
        ), patch(
            "ao_lore.workspace_runtime.recover_workspace", return_value={"operation": "recover"},
        ), patch(
            "ao_lore.workspace_query.WorkspaceGraphSnapshot", return_value=object(),
        ), patch(
            "ao_lore.workspace_query.WorkspaceQuerySnapshot", return_value=object(),
        ), patch(
            "ao_lore.workspace_query.query_workspace", return_value={"outcome": "answer"},
        ), patch("urllib.request.build_opener") as opener:
            for args in commands:
                with self.subTest(command=args.command):
                    _dispatch_workspace(args)
        opener.assert_not_called()

    def test_query_opens_only_primary_and_direct_reference_graphs(self):
        primary = {
            "workspace_id": "primary-fixture-a",
            "reference_workspace_ids": ["shared-reference-fixture"],
            "graph_id": "graph-primary-fixture-a",
        }
        unrelated = {
            "workspace_id": "secondary-fixture-b",
            "reference_workspace_ids": [],
            "graph_id": "graph-secondary-fixture-b",
        }
        reference = {
            "workspace_id": "shared-reference-fixture",
            "reference_workspace_ids": [],
            "graph_id": "graph-shared-reference-fixture",
        }
        generation = {"workspaces": [primary, unrelated, reference]}
        snapshot = SimpleNamespace(generations=(generation,))
        selection = SimpleNamespace(
            generation=generation, primary=primary, references=(reference,),
        )
        opened = []

        def graph_for(_home, definition):
            workspace_id = definition["workspace_id"]
            opened.append(workspace_id)
            if workspace_id == "secondary-fixture-b":
                raise FileNotFoundError("unrelated graph is deliberately absent")
            return {"graph_id": "graph-" + workspace_id}

        args = Namespace(
            command="query", workspace="primary-fixture-a", prompt="safe", json=True,
        )
        with patch("ao_lore.__main__.runtime_home", return_value=Path("/fixed/runtime")), patch(
            "ao_lore.workspace_registry.load_workspace_registry", return_value=snapshot,
        ), patch(
            "ao_lore.workspace_registry.select_workspace", return_value=selection,
        ), patch(
            "ao_lore.__main__._workspace_graph", side_effect=graph_for,
        ), patch(
            "ao_lore.__main__._workspace_freshness", return_value=None,
        ), patch(
            "ao_lore.workspace_query.WorkspaceGraphSnapshot",
            side_effect=lambda workspace_id, graph, _freshness: SimpleNamespace(
                workspace_id=workspace_id, graph=graph,
            ),
        ), patch(
            "ao_lore.workspace_query.WorkspaceQuerySnapshot", return_value=object(),
        ) as query_snapshot, patch(
            "ao_lore.workspace_query.query_workspace", return_value={"outcome": "answer"},
        ):
            report, context = _dispatch_workspace(args)

        self.assertEqual({"outcome": "answer"}, report)
        self.assertEqual(
            ["primary-fixture-a", "shared-reference-fixture"], opened,
        )
        query_snapshot.assert_called_once()
        self.assertEqual(
            {"primary-fixture-a", "shared-reference-fixture"}, context["selected"],
        )

    def test_refresh_default_transport_is_unavailable_after_policy_and_context_validation(self):
        definition = {
            "workspace_id": "primary-fixture-a",
            "reference_workspace_ids": [],
            "freshness_policy_id": "policy-fixture",
        }
        generation = {"workspaces": [definition]}
        snapshot = SimpleNamespace(generations=(generation,))
        selection = SimpleNamespace(
            generation=generation, primary=definition, references=(),
        )
        policy = {"sources": [{
            "source_id": "source-fixture",
            "canonical_locator": HTTPS + "source" + TEST_TLD + "/fixture",
            "prior_media_type": "text/html",
            "declared_successor_locator": None,
        }]}
        args = Namespace(
            command="refresh", workspace="primary-fixture-a", json=True,
        )
        with patch("ao_lore.__main__.runtime_home", return_value=Path("/fixed/runtime")), patch(
            "ao_lore.workspace_registry.load_workspace_registry", return_value=snapshot,
        ), patch(
            "ao_lore.workspace_registry.select_workspace", return_value=selection,
        ), patch(
            "ao_lore.__main__._workspace_graph", return_value={"graph_id": "graph-fixture"},
        ), patch(
            "ao_lore.__main__._workspace_freshness", return_value=None,
        ), patch(
            "ao_lore.__main__._workspace_state_json", return_value={"policy": "fixture"},
        ), patch(
            "ao_lore.evidence_graph_contracts.validate_freshness_policy", return_value=policy,
        ), patch(
            "ao_lore.workspace_runtime.open_workspace_context",
        ) as open_context, patch(
            "ao_lore.workspace_runtime.refresh_workspace",
        ) as refresh, patch("socket.socket") as socket_constructor, patch(
            "urllib.request.build_opener",
        ) as opener:
            code, out, err = self.invoke([
                "workspace", "refresh", "--workspace", "primary-fixture-a", "--json",
            ])
        self.assertEqual((2, "", "ao-lore: workspace operation rejected\n"), (code, out, err))
        open_context.assert_called_once()
        refresh.assert_not_called()
        socket_constructor.assert_not_called()
        opener.assert_not_called()

    def test_private_refresh_transport_seam_is_absent_by_default_and_injectable_for_tests(self):
        module = __import__("ao_lore.__main__", fromlist=["_workspace_refresh_transport"])
        self.assertIsNone(module._workspace_refresh_transport())
        adapter_source = inspect.getsource(module._workspace_refresh_adapter)
        self.assertNotIn("urllib", adapter_source)
        self.assertNotIn("BoundedClient", adapter_source)

        definition = {
            "workspace_id": "primary-fixture-a",
            "reference_workspace_ids": [],
            "freshness_policy_id": "policy-fixture",
        }
        selection = SimpleNamespace(primary=definition)
        dependencies = SimpleNamespace(runtime_root=Path("/fixed/runtime"))
        policy = {"sources": [{
            "source_id": "source-fixture",
            "canonical_locator": HTTPS + "docs.example" + TEST_TLD + "/fixture",
            "prior_media_type": "text/html",
            "declared_successor_locator": None,
        }]}
        transport = SimpleNamespace(fetch=Mock())
        with patch(
            "ao_lore.__main__._workspace_state_json", return_value={"policy": "fixture"},
        ), patch(
            "ao_lore.evidence_graph_contracts.validate_freshness_policy", return_value=policy,
        ), patch(
            "ao_lore.workspace_runtime.open_workspace_context",
        ) as open_context, patch(
            "ao_lore.__main__._workspace_refresh_transport", return_value=transport,
        ), patch(
            "ao_lore.workspace_runtime.refresh_workspace", return_value={"operation": "refresh"},
        ) as refresh:
            report = module._workspace_refresh_adapter(
                dependencies, selection, {"graph_id": "graph-fixture"},
            )
        self.assertEqual({"operation": "refresh"}, report)
        open_context.assert_called_once()
        self.assertIs(transport, refresh.call_args.kwargs["http"])
        transport.fetch.assert_not_called()

    def test_refresh_context_rejection_precedes_network_client_construction(self):
        definition = {
            "workspace_id": "primary-fixture-a",
            "reference_workspace_ids": [],
            "freshness_policy_id": "policy-fixture",
        }
        selection = SimpleNamespace(primary=definition)
        policy = {"sources": []}
        dependencies = SimpleNamespace(runtime_root=Path("/fixed/runtime"))
        with patch(
            "ao_lore.__main__._workspace_state_json", return_value={"policy": "fixture"},
        ), patch(
            "ao_lore.evidence_graph_contracts.validate_freshness_policy", return_value=policy,
        ), patch(
            "ao_lore.workspace_runtime.open_workspace_context",
            side_effect=ValueError("context rejected"),
        ) as open_context, patch(
            "ao_lore.__main__._workspace_refresh_transport",
        ) as transport:
            with self.assertRaisesRegex(ValueError, "context rejected"):
                __import__("ao_lore.__main__", fromlist=["_workspace_refresh_adapter"])._workspace_refresh_adapter(
                    dependencies, selection, {"graph_id": "graph-fixture"},
                )
        open_context.assert_called_once()
        transport.assert_not_called()


if __name__ == "__main__":
    unittest.main()
