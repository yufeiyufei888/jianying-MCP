"""Publishing boundary checks, not editor acceptance or content authorization."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import validate_release as audit


class PublicReleaseTests(unittest.TestCase):
    def test_real_public_manifest(self):
        self.assertEqual("passed", audit.validate()["status"])

    def test_personal_path_and_credential_patterns(self):
        self.assertTrue(audit.PRIVATE_PATH.search("X:" + "/Users/" + "fixture-user/private"))
        self.assertTrue(audit.TOKEN.search("ghp_" + "f" * 30))
        self.assertFalse(audit.PRIVATE_PATH.search("${USERPROFILE}/.codex/skills"))
        self.assertFalse(audit.TOKEN.search("FIXTURE_TOKEN_NOT_A_SECRET"))

    def test_unlisted_public_file_rejected(self):
        with patch.object(audit, "paths_to_check", return_value=audit.paths_to_check(audit.ROOT) | {"accidental.txt"}):
            with self.assertRaisesRegex(ValueError, "Whitelist mismatch"):
                audit.validate()

    def test_unsafe_whitelist_and_duplicate_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for files in [["../outside"], ["LICENSE", "LICENSE"]]:
                (root / "release-files.json").write_text(json.dumps({"schema": "jianying-public-files/1", "files": files}), encoding="utf-8")
                with self.assertRaises(ValueError):
                    audit.validate(root)

    def test_native_gate_not_in_manifest(self):
        manifest = json.loads((audit.ROOT / "release-files.json").read_text(encoding="utf-8"))
        self.assertFalse(any(Path(p).name in {"native_acceptance.json", "mcp_smoke.json", "receipt.json", "config.local.json"} for p in manifest["files"]))

    def test_cli_and_mcp_are_fixed_python_entrypoints(self):
        text = (audit.ROOT / "scripts/jianying_local/mcp_server.py").read_text(encoding="utf-8")
        self.assertIn('asyncio.create_subprocess_exec(*command', text)
        self.assertNotIn("create_subprocess_shell", text)
        self.assertNotIn("shell=True", text)


if __name__ == "__main__":
    unittest.main()
