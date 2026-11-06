import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("sdd_context", ROOT / "tools" / "sdd_context.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules["sdd_context"] = MODULE
SPEC.loader.exec_module(MODULE)


class SddContextTests(unittest.TestCase):
    def make_repo(self) -> Path:
        temp = Path(tempfile.mkdtemp(prefix=".sdd-test-", dir=ROOT))
        self.addCleanup(shutil.rmtree, temp, ignore_errors=True)
        (temp / ".sdd/specs").mkdir(parents=True)
        (temp / ".sdd/decisions").mkdir(parents=True)
        (temp / "src").mkdir()
        (temp / "tests").mkdir()
        (temp / "src/search.cpp").write_text("// full-text search context\n", encoding="utf-8")
        (temp / "tests/search.test").write_text("# full-text search regression\n", encoding="utf-8")
        return temp

    def test_pack_filters_superseded_and_finds_code_and_tests(self) -> None:
        root = self.make_repo()
        (root / ".sdd/specs/search.md").write_text(
            "---\nid: search-spec\nkind: spec\nstatus: active\nsummary: Full-text search contract\npaths: [src/search.cpp]\ntests: [tests/search.test]\n---\n",
            encoding="utf-8",
        )
        (root / ".sdd/decisions/old.md").write_text(
            "---\nid: old\nkind: decision\nstatus: superseded\nsummary: old search\n---\n",
            encoding="utf-8",
        )
        pack = MODULE.build_pack(root, "full-text search", 12000, 5)
        self.assertEqual(["search-spec"], [item["id"] for item in pack["documents"]])
        self.assertIn("src/search.cpp", [item["path"] for item in pack["files"]])
        self.assertIn("tests/search.test", pack["tests"])

    def test_budget_reports_omissions(self) -> None:
        root = self.make_repo()
        for index in range(4):
            (root / f".sdd/specs/{index}.md").write_text(
                f"---\nid: spec-{index}\nkind: spec\nstatus: active\nsummary: {'x' * 100}\n---\n", encoding="utf-8"
            )
        pack = MODULE.build_pack(root, "search", 200, 1)
        self.assertTrue(pack["omissions"])

    def test_check_rejects_missing_reference(self) -> None:
        root = self.make_repo()
        (root / ".sdd/specs/bad.md").write_text(
            "---\nid: bad\nkind: spec\nstatus: active\npaths: [src/missing.cpp]\n---\n", encoding="utf-8"
        )
        errors = MODULE.check_repository(root)
        self.assertTrue(any("missing referenced path" in error for error in errors))

    def test_json_output_is_serializable(self) -> None:
        root = self.make_repo()
        pack = MODULE.build_pack(root, "search", 1000, 2, handoff=True)
        json.dumps(pack, ensure_ascii=False)
        self.assertIn("handoff", pack)


if __name__ == "__main__":
    unittest.main()
