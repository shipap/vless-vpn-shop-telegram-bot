import importlib.util
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "dump_inventory.py"


class DumpInventoryTests(unittest.TestCase):
    def inspect(self, sql):
        spec = importlib.util.spec_from_file_location("dump_inventory", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.sql"
            path.write_text(sql, encoding="utf-8")
            return module.inspect_dump(path)

    def test_named_copy_counts_and_large_identity_digest(self):
        result = self.inspect(
            "COPY public.users (email, user_id, telegram_id, balance) FROM stdin;\n\\N\t9007199254740993\t\\N\t1.125\n\\.\n"
        )
        self.assertEqual(result["tables"]["users"]["count"], 1)
        self.assertEqual(result["tables"]["users"]["large_ids"], 1)
        self.assertEqual(result["tables"]["users"]["money_totals"]["balance"], "1.125")
        self.assertNotIn("9007199254740993", str(result))

    def test_active_sql_is_flagged_without_leaking_data(self):
        result = self.inspect(
            "COPY public.messages (text) FROM stdin;\nCREATE FUNCTION dangerous customer payload\n\\.\nCREATE FUNCTION public.x() RETURNS void LANGUAGE plpgsql AS $$BEGIN NULL; END$$;\n\\! arbitrary-secret\n"
        )
        self.assertEqual(result["tables"]["messages"]["count"], 1)
        self.assertEqual([i["kind"] for i in result["review_items"]], ["function", "shell_metacommand"])
        self.assertNotIn("arbitrary-secret", str(result))

    def test_unterminated_copy_rejected(self):
        with self.assertRaises(ValueError):
            self.inspect("COPY public.users (user_id) FROM stdin;\n1\n")


if __name__ == "__main__":
    unittest.main()
