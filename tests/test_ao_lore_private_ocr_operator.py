import importlib.util
import io
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "private-ocr-uat.py"


def load_script():
    spec = importlib.util.spec_from_file_location("private_ocr_uat_script", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError("operator script cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _TrackedStream(io.StringIO):
    def __init__(self):
        super().__init__()
        self.writes = 0
        self.flushes = 0

    def write(self, value):
        self.writes += 1
        return super().write(value)

    def flush(self):
        self.flushes += 1
        return super().flush()


class _ShortWriter(_TrackedStream):
    def write(self, value):
        self.writes += 1
        super().write(value[:3])
        return 3


class PrivateOcrOperatorTests(unittest.TestCase):
    def test_product_dispatcher_is_fixed_and_projects_no_private_state(self):
        from ao_lore import private_ocr_uat as product

        statuses = {
            "prepare-runtime": "prepared",
            "qualify": "qualified",
            "prepare-corpus": "prepared",
            "run": "completed",
            "cleanup": "cleaned",
        }
        for action, status in statuses.items():
            with self.subTest(action=action), patch.object(
                product, "_execute_private_ocr_action", return_value=status
            ) as execute:
                self.assertEqual(product.project_private_ocr_action(action), {
                    "action": action,
                    "status": status,
                    "network_accessed": False,
                    "provider_calls": False,
                    "promotion_authority": False,
                    "claims_authority_advance": False,
                })
                execute.assert_called_once_with(action)
        for invalid in ("", "prepare", "run ", 1, None):
            with self.subTest(invalid=invalid), self.assertRaises(Exception):
                product.project_private_ocr_action(invalid)

    def test_exposes_only_fixed_actions_and_optional_json_before_lazy_import(self):
        module = load_script()
        accepted = (
            ("prepare-runtime",), ("prepare-runtime", "--json"),
            ("--json", "qualify"), ("prepare-corpus",), ("run",), ("cleanup",),
        )
        service = SimpleNamespace(
            project=lambda action: {
                "action": action, "status": "ready", "network_accessed": False,
                "provider_calls": False, "promotion_authority": False,
                "claims_authority_advance": False,
            }
        )
        for argv in accepted:
            with self.subTest(argv=argv), patch.object(module, "_load_service", return_value=service):
                stdout, stderr = _TrackedStream(), _TrackedStream()
                with patch.object(sys, "stdout", stdout), patch.object(sys, "stderr", stderr):
                    self.assertEqual(module.main(argv), 0)
                self.assertEqual((stdout.writes, stdout.flushes), (1, 1))
                self.assertEqual(stderr.getvalue(), "")
        for option in ("--root", "--source", "--model", "--device", "--provider", "--retry"):
            with self.subTest(option=option), patch.object(module, "_load_service") as loader:
                stdout, stderr = _TrackedStream(), _TrackedStream()
                with patch.object(sys, "stdout", stdout), patch.object(sys, "stderr", stderr):
                    self.assertEqual(module.main(("run", option, "private")), 2)
                loader.assert_not_called()
                self.assertEqual(stderr.getvalue(), "ao-lore-private-ocr-uat: rejected\n")
                self.assertEqual((stderr.writes, stderr.flushes), (1, 1))

    def test_one_service_call_bounded_projection_and_content_free_failure(self):
        module = load_script()
        calls = []
        service = SimpleNamespace(project=lambda action: calls.append(action) or {
            "action": action, "status": "ready", "network_accessed": False,
            "provider_calls": False, "promotion_authority": False,
            "claims_authority_advance": False,
        })
        stdout, stderr = _TrackedStream(), _TrackedStream()
        with patch.object(module, "_load_service", return_value=service), \
             patch.object(sys, "stdout", stdout), patch.object(sys, "stderr", stderr):
            self.assertEqual(module.main(("cleanup", "--json")), 0)
        self.assertEqual(calls, ["cleanup"])
        self.assertNotIn("/", stdout.getvalue())

        private = SimpleNamespace(project=lambda action: (_ for _ in ()).throw(
            ValueError("/private/source.png TOKEN=secret")
        ))
        stdout, stderr = _TrackedStream(), _TrackedStream()
        with patch.object(module, "_load_service", return_value=private), \
             patch.object(sys, "stdout", stdout), patch.object(sys, "stderr", stderr):
            self.assertEqual(module.main(("run",)), 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "ao-lore-private-ocr-uat: rejected\n")

    def test_baseexception_propagates(self):
        module = load_script()
        service = SimpleNamespace(project=lambda action: (_ for _ in ()).throw(KeyboardInterrupt()))
        with patch.object(module, "_load_service", return_value=service):
            with self.assertRaises(KeyboardInterrupt):
                module.main(("run",))

    def test_short_write_and_aggregate_budget_never_report_success(self):
        module = load_script()
        value = {
            "action": "run", "status": "completed", "network_accessed": False,
            "provider_calls": False, "promotion_authority": False,
            "claims_authority_advance": False,
        }
        service = SimpleNamespace(project=lambda action: dict(value))
        stdout, stderr = _ShortWriter(), _TrackedStream()
        with patch.object(module, "_load_service", return_value=service), \
             patch.object(sys, "stdout", stdout), patch.object(sys, "stderr", stderr):
            self.assertEqual(module.main(("run", "--json")), 2)
        self.assertEqual(stderr.getvalue(), "ao-lore-private-ocr-uat: rejected\n")

        stdout, stderr = _TrackedStream(), _TrackedStream()
        with patch.object(module, "_load_service", return_value=service), \
             patch.object(module, "_MAX_RESULT_NODES", 5), \
             patch.object(sys, "stdout", stdout), patch.object(sys, "stderr", stderr):
            self.assertEqual(module.main(("run",)), 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "ao-lore-private-ocr-uat: rejected\n")


if __name__ == "__main__":
    unittest.main()
