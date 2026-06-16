import unittest
import os
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
