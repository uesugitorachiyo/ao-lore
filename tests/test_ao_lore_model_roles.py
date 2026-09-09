import copy
import unittest

from ao_lore.model_roles import (
    RoleConfigError,
    RoleExecutionError,
    RoleResult,
    RoleRuntime,
    ScriptedRoleAdapter,
    cache_identity,
    validate_role_configuration_set,
)


ROLES = ("parser", "distiller", "navigator", "synthesizer")


def config(role, adapter=None, model=None, credential=None, fallback=None):
    return {
        "schema_version": "ao.lore.model-role-config.v0.1",
        "role": role,
        "adapter": adapter or f"{role}-local",
        "provider": "local-runtime",
        "endpoint": None,
        "model_id": model or f"{role}-model",
        "policy_version": f"{role}-policy-v1",
        "output_schema_version": f"ao.lore.{role}-output.v0.1",
        "context_limit": 4096,
        "output_limit": 512,
        "temperature": 0,
        "decoding_controls": {"seed": 7},
        "timeout_ms": 1000,
        "retries": 0,
        "network_policy": "offline",
        "privacy_policy": "local-only",
        "credential_environment_variable": credential,
        "token_budget": 100,
        "monetary_budget": 0,
        "fallback_policy": fallback or {"enabled": False, "adapters": []},
        "cache_policy": {"enabled": True, "ttl_seconds": 60},
        "no_implicit_cross_role_inheritance": True,
    }


def configs():
    return {role: config(role) for role in ROLES}


def role_input(role):
    return {
        "parser": {"source_region": {"digest": "sha256:" + "a" * 64}, "parser_context": {}},
        "distiller": {"document_ir": {"schema_version": "ao.lore.document-ir.v0.1"}, "candidate_context": {}},
        "navigator": {"normalized_query": "policy", "routing_metadata": {}, "navigation_state": {}, "visited_nodes": [], "evidence_summary": {}, "coverage_gaps": [], "budgets": {}},
        "synthesizer": {"normalized_query": "policy", "evidence_ledger": [], "coverage_report": {}, "answer_format": "text", "citation_requirements": {}},
    }[role]


def successful(role, calls=None):
    def handler(payload, adapter_config):
        if calls is not None:
            calls.append((role, adapter_config["model_id"]))
        return RoleResult(
            output={"schema_version": adapter_config["output_schema_version"], "role": role},
            tokens=5,
            latency_ms=3,
            monetary_cost=0,
        )
    return handler


class RoleConfigurationTests(unittest.TestCase):
    def test_all_four_roles_are_independent_and_can_choose_different_models(self):
        values = configs()
        values["parser"]["model_id"] = "vision-local"
        values["distiller"]["model_id"] = "frontier-distill"
        values["navigator"]["model_id"] = "planner-local"
        values["synthesizer"]["model_id"] = "writer-frontier"
        validated = validate_role_configuration_set(values)
        self.assertEqual(len({item["model_id"] for item in validated.values()}), 4)
        self.assertIsNot(validated["parser"], validated["distiller"])

    def test_missing_field_or_inheritance_flag_fails_closed(self):
        values = configs()
        del values["navigator"]["token_budget"]
        with self.assertRaises(RoleConfigError):
            validate_role_configuration_set(values)
        values = configs()
        values["navigator"]["no_implicit_cross_role_inheritance"] = False
        with self.assertRaises(RoleConfigError):
            validate_role_configuration_set(values)

    def test_credential_environment_variable_cannot_be_reused_across_roles(self):
        values = configs()
        values["distiller"]["credential_environment_variable"] = "AO_SHARED_TOKEN"
        values["synthesizer"]["credential_environment_variable"] = "AO_SHARED_TOKEN"
        with self.assertRaises(RoleConfigError):
            validate_role_configuration_set(values)


class RoleRuntimeTests(unittest.TestCase):
    def adapters(self):
        return [ScriptedRoleAdapter(role, f"{role}-local", successful(role)) for role in ROLES]

    def test_shared_handler_does_not_merge_role_contracts(self):
        calls = []
        shared = lambda payload, cfg: RoleResult({"schema_version": cfg["output_schema_version"]}, 1, 1, 0)
        adapters = [ScriptedRoleAdapter(role, f"{role}-local", shared) for role in ROLES]
        runtime = RoleRuntime(configs(), adapters)
        for role in ROLES:
            result = runtime.execute(role, role_input(role), now_seconds=1)
            calls.append(result["trace"]["role"])
        self.assertEqual(calls, list(ROLES))

    def test_cross_role_fallback_is_rejected_before_execution(self):
        values = configs()
        values["parser"]["fallback_policy"] = {"enabled": True, "adapters": ["navigator-local"]}
        with self.assertRaises(RoleConfigError):
            RoleRuntime(values, self.adapters())

    def test_fallback_is_explicit_same_role_and_error_is_redacted(self):
        values = configs()
        values["navigator"]["fallback_policy"] = {"enabled": True, "adapters": ["navigator-backup"]}
        private_message = "provider transcript and secret should never persist"

        def fail(payload, cfg):
            raise RuntimeError(private_message)

        adapters = self.adapters()
        adapters.append(ScriptedRoleAdapter("navigator", "navigator-backup", successful("navigator")))
        adapters = [ScriptedRoleAdapter("navigator", "navigator-local", fail) if a.role == "navigator" and a.adapter_id == "navigator-local" else a for a in adapters]
        result = RoleRuntime(values, adapters).execute("navigator", role_input("navigator"), now_seconds=1)
        self.assertEqual(result["trace"]["adapter"], "navigator-backup")
        self.assertEqual(result["trace"]["fallback"]["from_adapter"], "navigator-local")
        self.assertNotIn(private_message, str(result))
        self.assertFalse(result["trace"]["private_reasoning_persisted"])

    def test_role_input_authority_is_strict(self):
        runtime = RoleRuntime(configs(), self.adapters())
        with self.assertRaises(RoleExecutionError):
            runtime.execute("distiller", {"source_document": b"forbidden"}, now_seconds=1)
        with self.assertRaises(RoleExecutionError):
            runtime.execute("synthesizer", {**role_input("synthesizer"), "browse_node": "brain/x.md"}, now_seconds=1)

    def test_cache_identity_includes_role_policy_and_configuration(self):
        values = configs()
        parser_key = cache_identity(values["parser"], role_input("parser"))
        changed = copy.deepcopy(values["parser"])
        changed["policy_version"] = "parser-policy-v2"
        self.assertNotEqual(parser_key, cache_identity(changed, role_input("parser")))
        changed["policy_version"] = values["parser"]["policy_version"]
        changed["role"] = "distiller"
        changed["output_schema_version"] = "ao.lore.distiller-output.v0.1"
        self.assertNotEqual(parser_key, cache_identity(changed, role_input("parser")))

    def test_cache_reuse_is_role_scoped_and_budget_is_enforced(self):
        calls = []
        adapters = [ScriptedRoleAdapter(role, f"{role}-local", successful(role, calls)) for role in ROLES]
        runtime = RoleRuntime(configs(), adapters)
        first = runtime.execute("parser", role_input("parser"), now_seconds=1)
        second = runtime.execute("parser", role_input("parser"), now_seconds=2)
        self.assertEqual(len(calls), 1)
        self.assertFalse(first["trace"]["fallback"]["cache_hit"])
        self.assertTrue(second["trace"]["fallback"]["cache_hit"])

        values = configs()
        values["parser"]["token_budget"] = 2
        with self.assertRaises(RoleExecutionError):
            RoleRuntime(values, adapters).execute("parser", role_input("parser"), now_seconds=1)


if __name__ == "__main__":
    unittest.main()
