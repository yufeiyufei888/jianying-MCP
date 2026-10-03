"""Native save rules are narrow; real edits and unknown fields remain errors."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from scripts.jianying_local import core, music, verification
from scripts.jianying_local.runtime import Settings, ToolError, canonical
from scripts.tests.test_jianying_local import fixture_doc


class SavedRulesTests(unittest.TestCase):
    def setUp(self):
        self.doc=fixture_doc(Path(tempfile.gettempdir()) / "original.mp4")
        self.doc["last_modified_platform"]={"app_version":"11.5.0"}

    def error(self, fn):
        with self.assertRaises(ToolError):fn()

    def audio(self):
        d=copy.deepcopy(self.doc)
        d["duration"]=8500000
        d["tracks"][0]["segments"][-1]["target_timerange"]["duration"]=4500000
        d["tracks"][0]["segments"][-1]["source_timerange"]["duration"]=4500000
        d["materials"]["audios"]=[{"id":"soundmat","type":"extract_music","path":str(Path(tempfile.gettempdir()) / "tone.wav"),"duration":8000000}]
        d["tracks"].append({"id":"soundtrack","type":"audio","segments":[{"id":"sound","material_id":"soundmat",
            "target_timerange":{"start":7766666,"duration":733334},"source_timerange":{"start":0,"duration":733334},
            "extra_material_refs":[],"speed":1,"volume":.25,"common_keyframes":[]}]})
        return d

    def text(self):
        d=copy.deepcopy(self.doc)
        d["materials"]["texts"]=[{"id":"caption","type":"subtitle","alignment":1,"line_max_width":.82,
            "content":json.dumps({"text":"字幕","styles":[{"font":{"path":str(Path(tempfile.gettempdir()) / "fixture-font.ttc")},"size":5}]})}]
        d["materials"]["speeds"].append({"id":"textspeed","speed":1})
        d["tracks"].append({"id":"captions","type":"text","segments":[{"id":"cue","material_id":"caption",
            "target_timerange":{"start":0,"duration":1000000},"extra_material_refs":["textspeed"],"enable_adjust":True}]})
        return d

    def test_native_identical_audio_alias_rows_are_preserved(self):
        d=self.audio();d["materials"]["audios"]*=3
        before=canonical(d)
        self.assertIn("soundmat",core.resource_map(d))
        self.assertEqual(before,canonical(d))

    def test_conflicting_or_unpinned_alias_is_rejected(self):
        d=self.audio();row=copy.deepcopy(d["materials"]["audios"][0]);row["duration"]-=1
        d["materials"]["audios"].append(row);self.error(lambda:core.resource_map(d))
        row["duration"]+=1;d["last_modified_platform"]={};self.error(lambda:core.resource_map(d))

    def test_cross_category_duplicate_is_rejected(self):
        d=self.audio();d["materials"]["videos"].append(copy.deepcopy(d["materials"]["audios"][0]))
        self.error(lambda:core.resource_map(d))

    def test_only_text_default_speed_can_disappear(self):
        e=self.text();a=copy.deepcopy(e);a["materials"]["speeds"].pop();a["tracks"][-1]["segments"][0]["extra_material_refs"]=[]
        self.assertEqual(1,verification.compare_saved(e,a)["native_removed_default_text_speeds"])
        e["materials"]["speeds"][-1]["speed"]=2;self.error(lambda:verification.compare_saved(e,a))
        e=self.doc;a=copy.deepcopy(e);a["materials"]["speeds"]=[]
        for s in a["tracks"][0]["segments"]:s["extra_material_refs"]=[]
        self.error(lambda:verification.compare_saved(e,a))

    def test_observed_empty_text_wrapper_but_not_unknown_change(self):
        e=self.text();a=copy.deepcopy(e);mat=a["materials"]["texts"][0]
        mat.pop("alignment");mat.pop("line_max_width")
        mat.update(caption_template_info={"path":"","resource_id":""},combo_info={},current_words={},
                   lyrics_template={"path":"","resource_id":""},shadow_point={"x":0.0,"y":0.0},translate_original_text="字幕")
        a["tracks"][-1]["segments"][0]["enable_adjust"]=False
        self.assertEqual("passed",verification.compare_saved(e,a)["semantic_validation"])
        mat["unknown_zero"]=0;self.error(lambda:verification.compare_saved(e,a))

    def test_styled_text_font_size_and_words_stay_exact(self):
        e=self.text();a=copy.deepcopy(e);a["materials"]["texts"][0]["content"]=json.dumps({"text":"改字","styles":[]})
        self.error(lambda:verification.compare_saved(e,a))

    def test_already_native_text_expectation_can_save_again(self):
        e=self.text();mat=e["materials"]["texts"][0]
        mat.update(caption_template_info={"path":"","resource_id":""},combo_info={},current_words={},
                   lyrics_template={"path":"","resource_id":""},shadow_point={"x":0.0,"y":0.0},translate_original_text="字幕")
        a=copy.deepcopy(e)
        self.assertEqual("passed",verification.compare_saved(e,a)["semantic_validation"])
        a["materials"]["texts"][0]["current_words"]={"unexpected":"payload"}
        self.error(lambda:verification.compare_saved(e,a))

    def test_audio_one_microsecond_source_representation_only(self):
        e=self.audio();a=copy.deepcopy(e);s=a["tracks"][-1]["segments"][0];s["source_timerange"]["duration"]-=1
        self.assertEqual(1,len(verification.compare_saved(e,a)["audio_source_microsecond_representations"]))
        s["target_timerange"]["start"]+=1;self.error(lambda:verification.compare_saved(e,a))

    def test_audio_moved_frame_or_changed_keyframe_is_rejected(self):
        e=self.audio();a=copy.deepcopy(e);a["tracks"][-1]["segments"][0]["target_timerange"]["start"]-=33333
        self.error(lambda:verification.compare_saved(e,a))
        a=copy.deepcopy(e);a["tracks"][-1]["segments"][0]["common_keyframes"]=[{"keyframe_list":[{"time_offset":1,"values":[.1]}]}]
        self.error(lambda:verification.compare_saved(e,a))

    def test_null_audio_hdr_removed_but_video_untouched(self):
        e=self.audio();e["tracks"][-1]["segments"][0]["hdr_settings"]=None;a=copy.deepcopy(e);a["tracks"][-1]["segments"][0].pop("hdr_settings")
        verification.compare_saved(e,a)
        e["tracks"][0]["segments"][0]["hdr_settings"]={"intensity":.5};self.error(lambda:verification.compare_saved(e,a))

    def items(self):
        return [{"path":"local.wav","start_us":0,"duration_us":8500000,"in_us":0,"loop_out_us":8000000,"loop":True,
                 "loop_crossfade_us":250000,"fade_in_us":400000,"fade_out_us":0,"volume":.25,"ducking":[]},
                {"path":"local.wav","start_us":8250000,"duration_us":3250000,"in_us":0,"loop":False,
                 "loop_crossfade_us":0,"crossfade_previous_us":250000,"fade_in_us":0,"fade_out_us":400000,"volume":.25,"ducking":[]}]

    def test_aligned_loop_preview_and_complementary_ramps(self):
        items=self.items();rows=music.schedule(items,fps=30)
        self.assertEqual([7750000,8500000],rows[1]["requested_range_us"])
        self.assertEqual([7766666,8500000],rows[1]["effective_range_us"])
        self.assertEqual(11500000,rows[-1]["start_us"]+rows[-1]["duration_us"])
        for index in (1,2):
            time=rows[index]["start_us"]+100000
            selected=rows[index-1:index+1]
            gains=[music.interpolate(music.envelope(items[r["item"]],r),time-r["start_us"]) for r in selected]
            self.assertAlmostEqual(.25,sum(gains),places=7)
        self.assertTrue(all(max(music.envelope(items[r["item"]],r)) <= r["duration_us"] for r in rows))

    def test_subframe_music_rejected(self):
        i=self.items()[0];i.update(duration_us=1,loop=False,fade_in_us=0)
        self.error(lambda:music.schedule([i],fps=30))

    def test_transition_even_frame_quantization_is_previewed(self):
        item={"after_segment_id":"left","before_segment_id":"right","resource_id":"fixture","duration_us":300000}
        def payload(x,*_):return {"id":"newtrans","resource_id":"fixture","duration":x["duration_us"]}
        with patch.object(core.backend,"transition_payload",side_effect=payload):
            changes=core.add_transitions(copy.deepcopy(self.doc),[item],Settings(canary=True),self.doc)
        self.assertEqual(300000,changes[0]["requested_duration_us"])
        self.assertEqual(266666,changes[0]["effective_duration_us"])


if __name__=="__main__":unittest.main()
