import unittest
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

# add project root to python path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.services import task as tm
from app.models.schema import MaterialInfo, VideoParams

resources_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "resources")

class TestTaskService(unittest.TestCase):
    def setUp(self):
        pass
    
    def tearDown(self):
        pass

    def test_generate_script_forwards_advanced_prompt_options(self):
        """
        任务生成入口和 WebUI/API 共用 VideoParams。这里验证自动生成文案时，
        高级提示词参数会继续传到 LLM 服务层，避免只在 /scripts 接口生效。
        """
        params = VideoParams(
            video_subject="咖啡",
            video_script="",
            video_language="zh-CN",
            paragraph_number=2,
            video_script_prompt="语气轻松",
            custom_system_prompt="Only write short narration.",
        )

        with patch.object(tm.llm, "generate_script", return_value="生成的文案") as generate:
            result = tm.generate_script("task-id", params)

        self.assertEqual(result, "生成的文案")
        generate.assert_called_once_with(
            video_subject="咖啡",
            language="zh-CN",
            paragraph_number=2,
            video_script_prompt="语气轻松",
            custom_system_prompt="Only write short narration.",
        )

    def test_generate_terms_uses_script_order_mode_when_enabled(self):
        """
        匹配模式下，关键词数量必须和脚本拆句数量一致，避免后续
        一句字幕拿不到对应的搜索词。
        """
        params = VideoParams(
            video_subject="城市通勤",
            video_script="",
            match_materials_to_script=True,
        )

        with patch.object(tm.llm, "generate_terms", return_value=["city", "train"]) as generate:
            result = tm.generate_terms("task-id", params, "先城市，再地铁")

        self.assertEqual(result, ["city", "train"])
        generate.assert_called_once_with(
            video_subject="城市通勤",
            video_script="先城市，再地铁",
            amount=2,
            match_script_order=True,
        )

    def test_generate_terms_regenerates_mismatched_manual_terms(self):
        params = VideoParams(
            video_subject="纽约旅行",
            video_script="",
            video_terms="city skyline",
            match_materials_to_script=True,
        )

        with patch.object(tm.llm, "generate_terms", return_value=["skyline", "park"]) as generate:
            result = tm.generate_terms("task-id", params, "See the skyline. Visit the park.")

        self.assertEqual(result, ["skyline", "park"])
        generate.assert_called_once_with(
            video_subject="纽约旅行",
            video_script="See the skyline. Visit the park.",
            amount=2,
            match_script_order=True,
        )

    def test_generate_terms_translates_chinese_terms_for_online_sources(self):
        params = VideoParams(
            video_subject="中国城市",
            video_script="",
            video_terms="广州城市天际线，重庆山城航拍",
            video_source="coverr",
            match_materials_to_script=True,
        )

        with patch.object(tm.llm, "generate_terms") as generate:
            result = tm.generate_terms(
                "task-id",
                params,
                "第五广州常住人口约1898万人。第四重庆常住人口约3190万人。",
            )

        self.assertEqual(result, ["Guangzhou city skyline", "Chongqing mountain city aerial"])
        generate.assert_not_called()

    def test_generate_terms_regenerates_mismatched_chinese_terms_for_online_sources(self):
        params = VideoParams(
            video_subject="中国城市",
            video_script="",
            video_terms="广州城市天际线",
            video_source="coverr",
            match_materials_to_script=True,
        )

        with patch.object(
            tm.llm, "generate_terms", return_value=["Guangzhou skyline", "Chongqing aerial"]
        ) as generate:
            result = tm.generate_terms(
                "task-id",
                params,
                "第五广州常住人口约1898万人。第四重庆常住人口约3190万人。",
            )

        self.assertEqual(result, ["Guangzhou skyline", "Chongqing aerial"])
        generate.assert_called_once()

    def test_chinese_script_with_english_voice_uses_chinese_fallback_voice(self):
        result = tm._resolve_voice_name_for_script(
            "en-AU-NatashaNeural-Female",
            "第五，广州，常住人口约1898万人。",
        )

        self.assertEqual(result, "zh-CN-XiaoxiaoNeural")

    def test_chinese_script_keeps_explicit_chinese_voice(self):
        result = tm._resolve_voice_name_for_script(
            "zh-CN-YunxiNeural-Male",
            "第五，广州，常住人口约1898万人。",
        )

        self.assertEqual(result, "zh-CN-YunxiNeural")

    def test_build_matched_segments_from_subtitle_timeline(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            subtitle_path = Path(temp_dir) / "subtitle.srt"
            subtitle_path.write_text(
                "1\n"
                "00:00:00,000 --> 00:00:02,500\n"
                "See the Statue of Liberty\n\n"
                "2\n"
                "00:00:02,500 --> 00:00:05,000\n"
                "Walk through Central Park\n\n",
                encoding="utf-8",
            )

            segments = tm.build_matched_segments(
                video_script="See the Statue of Liberty. Walk through Central Park.",
                video_terms=["Statue of Liberty", "Central Park"],
                subtitle_path=str(subtitle_path),
            )

        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0]["index"], 1)
        self.assertEqual(segments[0]["text"], "See the Statue of Liberty")
        self.assertEqual(segments[0]["term"], "Statue of Liberty")
        self.assertEqual(segments[0]["start"], 0.0)
        self.assertEqual(segments[0]["end"], 2.5)
        self.assertEqual(segments[0]["duration"], 2.5)
        self.assertEqual(segments[0]["material"], "")

    def test_write_audio_segment_files_decodes_mp3_without_ffprobe(self):
        """
        pydub calls ffprobe when MP3 metadata probing is left implicit. The editor
        flow should decode generated MP3 audio with an explicit codec so machines
        without ffprobe on PATH can still prepare sentence audio slices.
        """

        class _FakeAudio:
            def __len__(self):
                return 3000

            def __getitem__(self, key):
                return self

            def export(self, output_path, format):
                Path(output_path).write_bytes(b"fake-mp3")
                return None

        task_id = "mp3-no-ffprobe-task"
        task_dir = tm.utils.task_dir(task_id)
        try:
            with (
                patch("pydub.AudioSegment.from_file", return_value=_FakeAudio()) as from_file,
                patch.object(tm.voice, "_configure_pydub_ffmpeg"),
            ):
                segments, tail_pause = tm._write_audio_segment_files(
                    task_id,
                    "audio.mp3",
                    [
                        {
                            "index": 1,
                            "text": "One line.",
                            "start": 0.0,
                            "end": 1.0,
                        }
                    ],
                )

            from_file.assert_called_once_with("audio.mp3", format="mp3", codec="mp3")
            self.assertTrue(Path(segments[0]["audio_segment"]["file"]).exists())
            self.assertEqual(tail_pause, 2.0)
        finally:
            shutil.rmtree(task_dir, ignore_errors=True)

    def test_generate_final_videos_falls_back_without_matched_segments(self):
        params = VideoParams(
            video_subject="fallback",
            video_script="One sentence.",
            match_materials_to_script=True,
            video_count=1,
            video_source="pexels",
            video_concat_mode="random",
            video_transition_mode=None,
        )

        with (
            patch.object(tm.video, "combine_videos", return_value="combined.mp4") as combine,
            patch.object(tm.video, "combine_videos_by_segments") as combine_segments,
            patch.object(tm.video, "generate_video") as generate_video,
            patch.object(tm.sm.state, "update_task"),
        ):
            final_paths, combined_paths = tm.generate_final_videos(
                task_id="fallback-task",
                params=params,
                downloaded_videos=["material.mp4"],
                audio_file="audio.mp3",
                subtitle_path="",
                matched_segments=[],
            )

        combine.assert_called_once()
        combine_segments.assert_not_called()
        generate_video.assert_called_once()
        self.assertEqual(len(final_paths), 1)
        self.assertEqual(len(combined_paths), 1)

    def test_render_selection_rejects_unknown_candidate_id(self):
        """
        最终渲染接口只能接受 script.json 中已经准备好的 candidate_id，
        不能让前端提交任意文件路径绕过候选素材白名单。
        """
        task_id = "render-selection-invalid-candidate"
        task_dir = tm.utils.task_dir(task_id)
        params = VideoParams(
            video_subject="editor",
            video_script="One line.",
            video_terms="city",
            video_source="pexels",
            match_materials_to_script=True,
        )
        matched_segments = [
            {
                "index": 1,
                "text": "One line.",
                "term": "city",
                "start": 0.0,
                "end": 1.0,
                "duration": 1.0,
                "material": "/tmp/allowed.mp4",
                "candidates": [
                    {
                        "candidate_id": "seg-1-cand-1",
                        "rank": 1,
                        "material": "/tmp/allowed.mp4",
                        "source_url": "https://example.com/allowed.mp4",
                        "duration": 5,
                        "provider": "pexels",
                    }
                ],
                "audio_segment": {
                    "file": "/tmp/audio.mp3",
                    "pause_before": 0.0,
                    "original_text": "One line.",
                },
            }
        ]

        try:
            tm.save_script_data(
                task_id,
                "One line.",
                ["city"],
                params,
                matched_segments=matched_segments,
                extra={"audio_tail_pause": 0},
            )

            with self.assertRaises(ValueError):
                tm._render_selection_impl(
                    task_id,
                    [
                        {
                            "segment_index": 1,
                            "candidate_id": "not-from-this-task",
                            "trim_start": 0,
                            "trim_end": 1,
                            "text": "One line.",
                        }
                    ],
                )
        finally:
            shutil.rmtree(task_dir, ignore_errors=True)

    def test_materialize_remote_candidate_downloads_selected_source_once(self):
        task_id = "render-selection-downloads-selected"
        with tempfile.TemporaryDirectory() as temp_dir:
            task_root = os.path.join(temp_dir, task_id)

            def fake_task_dir(sub_dir=""):
                target = os.path.join(temp_dir, sub_dir) if sub_dir else temp_dir
                os.makedirs(target, exist_ok=True)
                return target

            def fake_save_video(video_url, save_dir=""):
                os.makedirs(save_dir, exist_ok=True)
                saved_path = os.path.join(save_dir, "selected.mp4")
                Path(saved_path).write_bytes(b"fake-video")
                return saved_path

            candidate = {
                "candidate_id": "seg-1-cand-1",
                "source_url": "https://v.example/selected.mp4",
                "preview_url": "https://v.example/selected.mp4",
                "material": "",
            }
            url_to_path = {}

            with (
                patch.object(tm.utils, "task_dir", side_effect=fake_task_dir),
                patch.object(tm.material, "save_video", side_effect=fake_save_video) as save_video,
            ):
                first_path = tm._materialize_remote_candidate(candidate, task_id, url_to_path)
                second_path = tm._materialize_remote_candidate(candidate, task_id, url_to_path)

            self.assertTrue(first_path.startswith(task_root))
            self.assertEqual(first_path, second_path)
            save_video.assert_called_once_with(
                video_url="https://v.example/selected.mp4",
                save_dir=os.path.join(task_root, "selected_materials"),
            )

    def test_cleanup_final_only_artifacts_removes_temp_inputs_and_combined_video(self):
        task_id = "cleanup-final-only"
        with tempfile.TemporaryDirectory() as temp_dir:
            task_root = os.path.join(temp_dir, task_id)
            render_dir = os.path.join(task_root, "render_materials")
            selected_dir = os.path.join(task_root, "selected_materials")
            os.makedirs(render_dir, exist_ok=True)
            os.makedirs(selected_dir, exist_ok=True)

            material_path = os.path.join(selected_dir, "source.mp4")
            combined_path = os.path.join(task_root, "combined-1.mp4")
            final_path = os.path.join(task_root, "final-1.mp4")
            extra_dir = os.path.join(task_root, "edited_audio_segments")
            os.makedirs(extra_dir, exist_ok=True)
            for file_path in (material_path, combined_path, final_path):
                Path(file_path).write_bytes(b"x")
            Path(os.path.join(extra_dir, "segment-001.mp3")).write_bytes(b"x")

            def fake_task_dir(sub_dir=""):
                target = os.path.join(temp_dir, sub_dir) if sub_dir else temp_dir
                os.makedirs(target, exist_ok=True)
                return target

            with patch.object(tm.utils, "task_dir", side_effect=fake_task_dir):
                tm._cleanup_final_only_artifacts(
                    task_id,
                    material_paths=[material_path, "https://v.example/source.mp4"],
                    combined_video_paths=[combined_path],
                    extra_paths=[extra_dir],
                )

            self.assertTrue(os.path.exists(final_path))
            self.assertFalse(os.path.exists(material_path))
            self.assertFalse(os.path.exists(combined_path))
            self.assertFalse(os.path.exists(render_dir))
            self.assertFalse(os.path.exists(selected_dir))
            self.assertFalse(os.path.exists(extra_dir))
    
    def test_task_local_materials(self):
        task_id = "00000000-0000-0000-0000-000000000000"
        video_materials=[]
        for i in range(1, 4):
            video_materials.append(MaterialInfo(
                provider="local",
                url=os.path.join(resources_dir, f"{i}.png"),
                duration=0
            ))

        params = VideoParams(
            video_subject="金钱的作用",
            video_script="金钱不仅是交换媒介，更是社会资源的分配工具。它能满足基本生存需求，如食物和住房，也能提供教育、医疗等提升生活品质的机会。拥有足够的金钱意味着更多选择权，比如职业自由或创业可能。但金钱的作用也有边界，它无法直接购买幸福、健康或真诚的人际关系。过度追逐财富可能导致价值观扭曲，忽视精神层面的需求。理想的状态是理性看待金钱，将其作为实现目标的工具而非终极目的。",
            video_terms="money importance, wealth and society, financial freedom, money and happiness, role of money",
            video_aspect="9:16",
            video_concat_mode="random",
            video_transition_mode="None",
            video_clip_duration=3,
            video_count=1,
            video_source="local",
            video_materials=video_materials,
            video_language="",
            voice_name="zh-CN-XiaoxiaoNeural-Female",
            voice_volume=1.0,
            voice_rate=1.0,
            bgm_type="random",
            bgm_file="",
            bgm_volume=0.2,
            subtitle_enabled=True,
            subtitle_position="bottom",
            custom_position=70.0,
            font_name="MicrosoftYaHeiBold.ttc",
            text_fore_color="#FFFFFF",
            text_background_color=True,
            font_size=60,
            stroke_color="#000000",
            stroke_width=1.5,
            n_threads=2,
            paragraph_number=1
        )
        result = tm.start(task_id=task_id, params=params)
        print(result)
    

if __name__ == "__main__":
    unittest.main()
