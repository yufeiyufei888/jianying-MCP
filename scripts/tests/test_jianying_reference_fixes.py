"""Regression tests for reference-only JianYing media fixes.

The tests load the installed skill source but stub MediaInfo and the draft runtime.
They use only temporary empty files and never open the GUI or touch a real draft.
"""

from __future__ import annotations

import importlib.util
import contextlib
import io
import os
import shutil
import sys
import tempfile
import types
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from scripts.jianying_local.runtime import Settings


SKILL_ROOT = Settings().skill_root
SCRIPTS = SKILL_ROOT / "scripts"
VENDOR = SCRIPTS / "vendor"
LOCAL_MATERIALS_PATH = VENDOR / "pyJianYingDraft" / "local_materials.py"
MEDIA_OPS_PATH = SCRIPTS / "core" / "media_ops.py"


class _FakeMediaInfo:
    """Tiny file-type-aware MediaInfo substitute; no bytes are inspected."""

    @staticmethod
    def can_parse() -> bool:
        return True

    @staticmethod
    def parse(path: str, **_kwargs):
        suffix = Path(path).suffix.lower()
        if suffix in {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"}:
            return SimpleNamespace(
                video_tracks=[],
                image_tracks=[],
                audio_tracks=[SimpleNamespace(duration=1250.0)],
            )
        return SimpleNamespace(
            video_tracks=[
                SimpleNamespace(duration=2400.0, width=1920, height=1080)
            ],
            image_tracks=[],
            audio_tracks=[],
        )


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load test target: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_skill_targets():
    if not LOCAL_MATERIALS_PATH.is_file() or not MEDIA_OPS_PATH.is_file():
        raise FileNotFoundError(
            "jianying-editor source not found; set JY_SKILL_ROOT to its install root"
        )

    original_sys_path = list(sys.path)
    fake_modules = {}
    with mock.patch.dict(sys.modules, fake_modules):
        try:
            sys.path.insert(0, str(SCRIPTS))
            sys.path.insert(0, str(VENDOR))

            # Isolate import-time optional dependencies. These fakes never inspect media.
            pymediainfo = types.ModuleType("pymediainfo")
            pymediainfo.MediaInfo = _FakeMediaInfo
            sys.modules["pymediainfo"] = pymediainfo
            local_materials = _load_module(
                "_jianying_reference_test_local_materials", LOCAL_MATERIALS_PATH
            )

            draft = types.ModuleType("pyJianYingDraft")
            draft.TrackType = SimpleNamespace(video="video", audio="audio")
            draft.VideoMaterial = local_materials.VideoMaterial
            draft.AudioMaterial = local_materials.AudioMaterial
            draft.Timerange = lambda start, duration: (start, duration)

            class _Segment:
                def __init__(self, material, target_timerange, source_timerange=None):
                    self.material = material
                    self.target_timerange = target_timerange
                    self.source_timerange = source_timerange

                def overlaps(self, _other):
                    return False

            draft.VideoSegment = _Segment
            draft.AudioSegment = _Segment
            draft.trange = lambda start, duration: (start, duration)
            sys.modules["pyJianYingDraft"] = draft
            exceptions = types.ModuleType("pyJianYingDraft.exceptions")

            class SegmentOverlap(Exception):
                pass

            exceptions.SegmentOverlap = SegmentOverlap
            sys.modules["pyJianYingDraft.exceptions"] = exceptions

            # Stub just the two utility modules imported by media_ops.
            utils = types.ModuleType("utils")
            utils.__path__ = []
            sys.modules["utils"] = utils
            formatters = types.ModuleType("utils.formatters")

            def safe_tim(value):
                if value is None:
                    return 0
                if isinstance(value, str):
                    value = value.strip()
                    if value.endswith("s"):
                        return int(float(value[:-1]) * 1_000_000)
                if isinstance(value, float):
                    return int(value * 1_000_000)
                return int(value)

            formatters.safe_tim = safe_tim
            formatters.get_duration_ffprobe_cached = lambda _path: 0.0
            sys.modules["utils.formatters"] = formatters
            normalizer = types.ModuleType("utils.media_normalizer")
            normalizer.normalize_webm_for_jianying = lambda _path: None
            sys.modules["utils.media_normalizer"] = normalizer

            media_ops = _load_module(
                "_jianying_reference_test_media_ops", MEDIA_OPS_PATH
            )
            # Returned objects retain references to their stub dependencies even
            # after patch.dict restores the interpreter's original module table.
            return local_materials, media_ops
        finally:
            sys.path[:] = original_sys_path


BACKEND_AVAILABLE = LOCAL_MATERIALS_PATH.is_file() and MEDIA_OPS_PATH.is_file()
if BACKEND_AVAILABLE:
    LOCAL_MATERIALS, MEDIA_OPS = _load_skill_targets()
else:
    LOCAL_MATERIALS, MEDIA_OPS = None, SimpleNamespace(MediaOpsMixin=object)


class _FakeTrack:
    def __init__(self, track_type):
        self.track_type = track_type
        self.segments = []


class _FakeScript:
    def __init__(self):
        self.tracks = {}
        self.added_segments = []
        self.width = 0
        self.height = 0

    def add_track(self, track_type, name):
        self.tracks[name] = _FakeTrack(track_type)

    def add_segment(self, segment, track_name):
        self.tracks[track_name].segments.append(segment)
        self.added_segments.append((segment, track_name))


class _ProjectHarness(MEDIA_OPS.MediaOpsMixin):
    def __init__(self):
        self.script = _FakeScript()
        self._explicit_res = True
        self._first_video_resolved = False
        self._cloud_audio_patches = {}
        self.cloud_manager = None

    def get_track_duration(self, _track_name):
        return 0


@unittest.skipUnless(BACKEND_AVAILABLE, "External reference-fixed backend unavailable; all 11 regression cases retained")
class JianyingReferenceFixTests(unittest.TestCase):
    def test_same_absolute_path_has_stable_nonempty_id_and_export_agreement(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            video_path = Path(temp_dir) / "clip.mp4"
            video_path.touch()
            expected = uuid.uuid5(
                uuid.NAMESPACE_URL,
                "jianying-local-material:" + os.path.normcase(os.path.abspath(video_path)),
            ).hex

            first = LOCAL_MATERIALS.VideoMaterial(str(video_path))
            second = LOCAL_MATERIALS.VideoMaterial(str(video_path))
            exported = first.export_json()

            self.assertTrue(first.local_material_id)
            self.assertEqual(first.local_material_id, second.local_material_id)
            self.assertEqual(first.local_material_id, expected)
            self.assertEqual(exported["local_material_id"], first.local_material_id)
            self.assertEqual(exported["id"], first.material_id)
            self.assertEqual(exported["material_id"], first.material_id)

    def test_different_directories_with_same_basename_do_not_collide(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            first_path = Path(temp_dir) / "one" / "same.mp4"
            second_path = Path(temp_dir) / "two" / "same.mp4"
            first_path.parent.mkdir()
            second_path.parent.mkdir()
            first_path.touch()
            second_path.touch()

            first = LOCAL_MATERIALS.VideoMaterial(str(first_path))
            second = LOCAL_MATERIALS.VideoMaterial(str(second_path))

            self.assertTrue(first.local_material_id)
            self.assertTrue(second.local_material_id)
            self.assertNotEqual(first.local_material_id, second.local_material_id)

    def test_audio_local_id_matches_its_exported_material_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            audio_path = Path(temp_dir) / "voice.wav"
            audio_path.touch()

            audio = LOCAL_MATERIALS.AudioMaterial(str(audio_path))
            exported = audio.export_json()
            expected = uuid.uuid5(
                uuid.NAMESPACE_URL,
                "jianying-local-material:" + os.path.normcase(os.path.abspath(audio_path)),
            ).hex

            self.assertTrue(getattr(audio, "local_material_id", ""))
            self.assertEqual(audio.local_material_id, expected)
            self.assertEqual(exported["local_material_id"], audio.local_material_id)
            self.assertEqual(exported["id"], audio.material_id)
            self.assertEqual(exported["music_id"], audio.material_id)

    def test_successful_local_audio_still_adds_one_audio_segment(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            audio_path = Path(temp_dir) / "music.wav"
            audio_path.write_bytes(b"test placeholder")
            project = _ProjectHarness()

            segment = project.add_audio_safe(
                str(audio_path), start_time=0, duration="1s", track_name="BGM"
            )

            self.assertIsNotNone(segment)
            self.assertIn("BGM", project.script.tracks)
            self.assertEqual(project.script.tracks["BGM"].track_type, "audio")
            self.assertEqual(project.script.added_segments, [(segment, "BGM")])
            self.assertEqual(segment.material.path, str(audio_path.resolve()))

    def test_cloud_music_download_failure_returns_none_without_placeholder_or_track(self):
        class FailedCloudManager:
            def __init__(self):
                self.download_calls = []

            def download_asset(self, query):
                self.download_calls.append(query)
                return None

            def get_asset_duration(self, _query):
                raise AssertionError("duration lookup is not needed after download failure")

        project = _ProjectHarness()
        manager = FailedCloudManager()
        project.cloud_manager = manager

        with contextlib.redirect_stdout(io.StringIO()):
            result = project.add_cloud_music("cloud-song-id", start_time=0)

        self.assertIsNone(result)
        self.assertEqual(manager.download_calls, ["cloud-song-id"])
        self.assertEqual(project.script.tracks, {})
        self.assertEqual(project.script.added_segments, [])
        self.assertEqual(project._cloud_audio_patches, {})

    def test_invalid_cloud_download_results_do_not_create_tracks_or_patches(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            results = [str(Path(temp_dir) / "missing.mp3"), temp_dir]
            for result_path in results:
                with self.subTest(result_path=result_path):
                    class InvalidPathCloudManager:
                        def download_asset(self, _query):
                            return result_path

                        def get_asset_duration(self, _query):
                            raise AssertionError("invalid downloads must fail before lookup")

                    project = _ProjectHarness()
                    project.cloud_manager = InvalidPathCloudManager()

                    with contextlib.redirect_stdout(io.StringIO()):
                        result = project.add_cloud_music("cloud-song-id", start_time=0)

                    self.assertIsNone(result)
                    self.assertEqual(project.script.tracks, {})
                    self.assertEqual(project.script.added_segments, [])
                    self.assertEqual(project._cloud_audio_patches, {})

    def test_successful_cloud_music_download_adds_exactly_one_real_audio_segment(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            audio_path = Path(temp_dir) / "downloaded.mp3"
            audio_path.write_bytes(b"temporary audio marker")

            class SuccessfulCloudManager:
                def __init__(self):
                    self.download_calls = []

                def download_asset(self, query):
                    self.download_calls.append(query)
                    return str(audio_path)

            project = _ProjectHarness()
            manager = SuccessfulCloudManager()
            project.cloud_manager = manager

            segment = project.add_cloud_music("cloud-song-id", start_time=0)

            self.assertIsNotNone(segment)
            self.assertEqual(manager.download_calls, ["cloud-song-id"])
            self.assertEqual(list(project.script.tracks), ["BGM"])
            self.assertEqual(project.script.tracks["BGM"].track_type, "audio")
            self.assertEqual(project.script.added_segments, [(segment, "BGM")])
            self.assertEqual(segment.material.path, str(audio_path.resolve()))
            self.assertEqual(project._cloud_audio_patches, {})

    def test_downloaded_cloud_audio_parse_failure_returns_none_without_side_effects(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            audio_path = Path(temp_dir) / "downloaded-but-unparseable.mp3"
            audio_path.write_bytes(b"temporary audio marker")

            class DownloadedCloudManager:
                def download_asset(self, _query):
                    return str(audio_path)

            project = _ProjectHarness()
            project.cloud_manager = DownloadedCloudManager()

            with mock.patch.object(
                _FakeMediaInfo, "parse", side_effect=RuntimeError("simulated parse failure")
            ):
                result = project.add_cloud_music("cloud-song-id", start_time=0)

            self.assertIsNone(result)
            self.assertEqual(project.script.tracks, {})
            self.assertEqual(project.script.added_segments, [])
            self.assertEqual(project._cloud_audio_patches, {})

    @unittest.skipUnless(os.name == "nt", "Windows path case normalization only")
    def test_windows_relative_absolute_and_case_variants_have_same_local_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            previous_cwd = os.getcwd()
            try:
                os.chdir(temp_dir)
                video_path = Path(temp_dir) / "ClipCase.mp4"
                video_path.touch()
                relative = "ClipCase.mp4"
                absolute = str(video_path.resolve())
                alternate_case = absolute.swapcase()

                relative_material = LOCAL_MATERIALS.VideoMaterial(relative)
                absolute_material = LOCAL_MATERIALS.VideoMaterial(absolute)
                case_material = LOCAL_MATERIALS.VideoMaterial(alternate_case)

                self.assertEqual(relative_material.path, absolute_material.path)
                self.assertEqual(relative_material.local_material_id, absolute_material.local_material_id)
                self.assertEqual(absolute_material.local_material_id, case_material.local_material_id)
            finally:
                os.chdir(previous_cwd)

    def test_loading_test_targets_does_not_pollute_sys_modules_or_sys_path(self):
        path_before = list(sys.path)
        modules_before = set(sys.modules)

        _load_skill_targets()

        self.assertEqual(sys.path, path_before)
        self.assertEqual(set(sys.modules) - modules_before, set())

    def test_local_audio_and_video_reference_original_paths_without_copying(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video_path = root / "source.mp4"
            audio_path = root / "source.wav"
            video_path.write_bytes(b"video marker")
            audio_path.write_bytes(b"audio marker")
            before = {
                p.name: (p.stat().st_size, p.read_bytes())
                for p in (video_path, audio_path)
            }
            project = _ProjectHarness()

            with (
                mock.patch.object(shutil, "copy", side_effect=AssertionError("copy called")),
                mock.patch.object(shutil, "copy2", side_effect=AssertionError("copy2 called")),
                mock.patch.object(shutil, "copyfile", side_effect=AssertionError("copyfile called")),
            ):
                video_segment = project.add_media_safe(
                    str(video_path), start_time=0, duration="1s", track_name="VideoTrack"
                )
                audio_segment = project.add_media_safe(
                    str(audio_path), start_time=0, duration="1s", track_name="AudioTrack"
                )

            self.assertEqual(video_segment.material.path, str(video_path.resolve()))
            self.assertEqual(audio_segment.material.path, str(audio_path.resolve()))
            self.assertEqual(
                {
                    p.name: (p.stat().st_size, p.read_bytes())
                    for p in (video_path, audio_path)
                },
                before,
            )
            self.assertEqual({p.name for p in root.iterdir()}, {"source.mp4", "source.wav"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
