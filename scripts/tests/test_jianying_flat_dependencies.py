"""The flat and mirrored readers share the native dependency safety boundary."""
import copy
import json
import unittest
from pathlib import Path

from scripts.jianying_local import core, verification
from scripts.jianying_local.runtime import canonical
from scripts.tests import test_jianying_local as fixtures


class FlatDependencyTests(unittest.TestCase):
    setUp = fixtures.PipelineTests.setUp
    fake_probe = fixtures.PipelineTests.fake_probe
    fake_build = fixtures.PipelineTests.fake_build
    fake_music = fixtures.PipelineTests.fake_music
    fake_transition = fixtures.PipelineTests.fake_transition
    request = fixtures.PipelineTests.request
    apply = fixtures.PipelineTests.apply
    error = fixtures.PipelineTests.error

    def write_doc(self, mirrored=False):
        (self.native / "source" / "draft_info.json").write_bytes(canonical(self.doc))
        if mirrored:
            (self.native / "source" / "draft_content.json").write_bytes(canonical(self.doc))

    def font(self):
        font = self.root / "local-font.ttf"
        font.write_bytes(b"test-only-font-dependency")
        self.doc["materials"]["texts"] = [{"id": "test_text", "font_path": str(font),
            "content": json.dumps({"text": "中文", "styles": [{"font": {"path": str(font)}}]})}]
        return font

    def effect(self):
        effect = self.root / "effect-cache"
        effect.mkdir()
        (effect / "config.json").write_bytes(b'{"fixture":true}')
        self.doc["materials"]["transitions"] = [{"id": "existing_transition", "path": str(effect),
            "resource_id": "test-cache", "duration": 266666}]
        return effect

    def test_flat_and_mirrored_fonts_are_bound_and_verifiable(self):
        font = self.font()
        for mirrored in (False, True):
            self.write_doc(mirrored)
            source = core.read_source(self.settings, "source")
            self.assertEqual(1, sum(d.get("path") == str(font) for d in source["dependencies"]))
        before = copy.deepcopy(self.doc)
        preview, _ = self.apply()
        self.assertEqual("passed", core.verify_draft(self.settings, "test-copy", preview["plan_id"])["file_validation"])
        self.assertEqual(before, core.read_source(self.settings, "source")["doc"])

    def test_effect_tree_flat_mirrored_copy_and_stale_dependency(self):
        effect = self.effect()
        for mirrored in (False, True):
            self.write_doc(mirrored)
            source = core.read_source(self.settings, "source")
            self.assertTrue(any(d.get("directory") == str(effect) for d in source["dependencies"]))
        preview, _ = self.apply()
        self.assertEqual("passed", core.verify_draft(self.settings, "test-copy", preview["plan_id"])["boundary_validation"])
        request = self.request()
        request["name"] = "later-copy"
        preview = core.plan_draft(self.settings, request)
        (effect / "config.json").write_bytes(b'{"fixture":false}')
        self.error("stale_input", lambda: core.apply_plan(self.settings, preview["plan_id"], preview["plan_sha256"]))
        self.assertFalse((self.native / "later-copy").exists())

    def test_unknown_flat_field_cannot_reference_arbitrary_dependency(self):
        self.doc["unknown_top"]["cache_path"] = str(self.music)
        self.write_doc(True)
        self.error("unknown_dependency", lambda: core.read_source(self.settings, "source"))

    def test_internal_font_rejected_for_both_read_modes(self):
        font = self.native / "font.ttf"
        font.write_bytes(b"not-an-external-font")
        self.doc["materials"]["texts"] = [{"id": "text", "font_path": str(font)}]
        self.write_doc(True)
        for diagnostic in (False, True):
            self.error("internal_dependency", lambda: core.read_source(self.settings, "source", allow_missing=diagnostic))

    def test_internal_cover_without_layout_binding_rejected(self):
        folder = self.native / "source"
        self.meta["draft_cover"] = str(folder / "draft_cover.jpg")
        (folder / "draft_meta_info.json").write_bytes(canonical(self.meta))
        # The flat metadata layout cannot silently copy an internal cover.
        # Test inventory directly; files_of additionally rejects this layout.
        (folder / "draft_cover.jpg").write_bytes(b"test-only-cover")
        self.error("internal_dependency", lambda: core.deps_io.inventory(
            self.doc, self.meta, folder, self.settings, persist=False, probe_fn=self.fake_probe))

    def test_non_effect_directory_and_missing_font_rejected(self):
        effect = self.root / "not-a-cover-directory"
        effect.mkdir()
        self.doc["cover_path"] = str(effect)
        self.write_doc()
        self.error("unknown_dependency", lambda: core.read_source(self.settings, "source"))
        self.doc.pop("cover_path")
        font = self.font()
        self.write_doc(True)
        font.unlink()
        self.error("missing_dependency", lambda: core.read_source(self.settings, "source"))

    def test_reparse_effect_descendant_rejected_if_creation_available(self):
        effect = self.effect()
        link = effect / "link.ttf"
        try:
            link.symlink_to(self.music)
        except OSError:
            self.skipTest("Windows symlink creation unavailable")
        self.write_doc(True)
        self.error("reparse_path", lambda: core.read_source(self.settings, "source"))

    def test_absent_effect_path_is_not_a_cache_equivalence_proof(self):
        self.assertFalse(verification.equivalent_effect_paths(None, str(self.root), "6724845717472416269"))
        self.assertFalse(verification.equivalent_effect_paths("", str(self.root), "6724845717472416269"))

    def test_missing_flat_generated_thumbnail_is_listed_not_fabricated(self):
        self.meta["draft_cover"] = "draft_cover.jpg"
        folder = self.native / "source"
        (folder / "draft_meta_info.json").write_bytes(canonical(self.meta))
        self.write_doc(True)
        for diagnostic in (False, True):
            source = core.read_source(self.settings, "source", allow_missing=diagnostic)
            self.assertEqual(["draft_cover.jpg"], source["ignored"])
        before = (folder / "draft_meta_info.json").read_bytes()
        preview, receipt = self.apply()
        self.assertEqual(["draft_cover.jpg"], preview["preview"]["omitted_regenerable_files"])
        target = Path(receipt["draft_path"])
        self.assertFalse((target / "draft_cover.jpg").exists())
        self.assertEqual("draft_cover.jpg", core.read_source(self.settings, "test-copy")["meta"]["draft_cover"])
        self.assertEqual(before, (folder / "draft_meta_info.json").read_bytes())
        self.assertEqual("passed", core.verify_draft(self.settings, "test-copy", preview["plan_id"])["file_validation"])

    def test_missing_absolute_cover_and_content_assets_are_not_optional(self):
        self.meta["draft_cover"] = str(self.native / "source" / "draft_cover.jpg")
        (self.native / "source" / "draft_meta_info.json").write_bytes(canonical(self.meta))
        self.write_doc(True)
        self.error("missing_dependency", lambda: core.read_source(self.settings, "source"))
        self.meta["draft_cover"] = "draft_cover.jpg"
        (self.native / "source" / "draft_meta_info.json").write_bytes(canonical(self.meta))
        self.doc["static_cover_image_path"] = str(self.root / "missing-render-asset.jpg")
        self.write_doc(True)
        self.error("missing_dependency", lambda: core.read_source(self.settings, "source"))

    def test_known_thumbnail_appearing_after_preview_invalidates_source(self):
        self.meta["draft_cover"] = "draft_cover.png"
        folder = self.native / "source"
        (folder / "draft_meta_info.json").write_bytes(canonical(self.meta))
        self.write_doc(True)
        preview = core.plan_draft(self.settings, self.request())
        (folder / "draft_cover.png").write_bytes(b"now-a-real-thumbnail")
        self.error("stale_input", lambda: core.apply_plan(self.settings, preview["plan_id"], preview["plan_sha256"]))
        self.assertFalse((self.native / "test-copy").exists())

    def test_missing_generated_thumbnail_is_not_optional_without_flat_permission(self):
        folder = self.native / "source"
        self.meta["draft_cover"] = "draft_cover.jpg"
        self.error("missing_dependency", lambda: core.deps_io.inventory(
            self.doc, self.meta, folder, self.settings, persist=False, probe_fn=self.fake_probe))


if __name__ == "__main__":
    unittest.main()
