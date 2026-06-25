import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests
from PIL import Image, ImageFilter

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.config import config
from app.services import material


class TestMaterialTlsVerification(unittest.TestCase):
    def setUp(self):
        self.original_app_config = dict(config.app)
        self.original_proxy_config = dict(config.proxy)

    def tearDown(self):
        config.app.clear()
        config.app.update(self.original_app_config)
        config.proxy.clear()
        config.proxy.update(self.original_proxy_config)

    def test_search_pexels_uses_tls_verification_by_default(self):
        """
        默认路径必须开启 TLS 校验，避免素材 API key 和返回的素材 URL
        在公共网络或不可信代理环境中被中间人攻击截获或篡改。
        """
        config.app["pexels_api_keys"] = ["pexels-key"]
        config.app.pop("tls_verify", None)
        config.proxy.clear()

        fake_response = SimpleNamespace(
            json=lambda: {
                "videos": [
                    {
                        "duration": 8,
                        "video_files": [
                            {
                                "width": 1080,
                                "height": 1920,
                                "link": "https://example.com/video.mp4",
                            }
                        ],
                    }
                ]
            }
        )

        with patch("app.services.material.requests.get", return_value=fake_response) as get:
            results = material.search_videos_pexels("cat", minimum_duration=1)

        self.assertEqual(len(results), 1)
        self.assertTrue(get.call_args.kwargs["verify"])

    def test_search_pexels_relaxed_mode_keeps_landscape_without_orientation_filter(self):
        """
        Candidate preview search should not reject landscape clips just because
        the output video is portrait; final rendering can resize the selected clip.
        """
        config.app["pexels_api_keys"] = ["pexels-key"]
        config.app.pop("tls_verify", None)
        config.proxy.clear()

        fake_response = SimpleNamespace(
            json=lambda: {
                "videos": [
                    {
                        "duration": 8,
                        "url": "https://www.pexels.com/video/tiananmen-square-1/",
                        "image": "https://images.pexels.com/video-files/1.jpg",
                        "video_files": [
                            {
                                "width": 1920,
                                "height": 1080,
                                "link": "https://example.com/landscape.mp4",
                            }
                        ],
                    }
                ]
            }
        )

        with patch("app.services.material.requests.get", return_value=fake_response) as get:
            results = material.search_videos_pexels(
                "Beijing Tiananmen Square",
                minimum_duration=1,
                exact_resolution=False,
                use_orientation_filter=False,
            )

        self.assertEqual(len(results), 1)
        self.assertNotIn("orientation=", get.call_args.args[0])
        self.assertEqual(results[0].url, "https://example.com/landscape.mp4")
        self.assertEqual(results[0].width, 1920)
        self.assertEqual(results[0].height, 1080)
        self.assertEqual(
            results[0].source_page_url,
            "https://www.pexels.com/video/tiananmen-square-1/",
        )

    def test_search_pixabay_allows_explicit_tls_disable_for_proxy(self):
        """
        少数企业代理会使用自签证书。该场景必须显式配置关闭 TLS 校验，
        不能再由代码硬编码默认关闭。
        """
        config.app["pixabay_api_keys"] = ["pixabay-key"]
        config.app["tls_verify"] = False
        config.proxy.clear()

        fake_response = SimpleNamespace(
            json=lambda: {
                "hits": [
                    {
                        "duration": 8,
                        "videos": {
                            "large": {
                                "width": 1920,
                                "url": "https://example.com/video.mp4",
                            }
                        },
                    }
                ]
            }
        )

        with patch("app.services.material.requests.get", return_value=fake_response) as get:
            results = material.search_videos_pixabay("cat", minimum_duration=1)

        self.assertEqual(len(results), 1)
        self.assertFalse(get.call_args.kwargs["verify"])

    def test_save_video_uses_tls_verification_by_default(self):
        config.app.pop("tls_verify", None)
        config.proxy.clear()

        fake_response = SimpleNamespace(content=b"fake-video")

        class FakeVideoFileClip:
            duration = 1
            fps = 24

            def __init__(self, path):
                self.path = path

            def close(self):
                return None

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch(
                "app.services.material.requests.get", return_value=fake_response
            ) as get, patch("app.services.material.VideoFileClip", FakeVideoFileClip):
                video_path = material.save_video(
                    "https://example.com/video.mp4?token=abc", save_dir=temp_dir
                )

            self.assertTrue(os.path.exists(video_path))
            self.assertTrue(get.call_args.kwargs["verify"])

    def test_download_videos_accepts_plain_string_concat_mode(self):
        """
        download_videos 可能被服务层或测试直接传入字符串模式，而不是
        VideoConcatMode 枚举。这里用空搜索词避免真实网络请求，只验证
        字符串 "random" 不会再因为访问 `.value` 抛 AttributeError。
        """
        result = material.download_videos(
            task_id="string-concat-mode",
            search_terms=[],
            video_concat_mode="random",
        )

        self.assertEqual(result, [])

    def test_download_videos_can_round_robin_terms_in_script_order(self):
        """
        开启按文案顺序匹配素材后，不能让第一个关键词的多个候选先把
        音频时长填满。这里模拟两个关键词各有多个候选，验证下载顺序是
        term1-第1个、term2-第1个、term1-第2个，贴近脚本叙事顺序。
        """
        search_results = {
            "opening city": [
                material.MaterialInfo(provider="pexels", url="https://v.example/a1.mp4", duration=3),
                material.MaterialInfo(provider="pexels", url="https://v.example/a2.mp4", duration=3),
            ],
            "middle office": [
                material.MaterialInfo(provider="pexels", url="https://v.example/b1.mp4", duration=3),
                material.MaterialInfo(provider="pexels", url="https://v.example/b2.mp4", duration=3),
            ],
        }
        downloaded_urls = []

        def fake_search(search_term, minimum_duration, video_aspect, **kwargs):
            return search_results[search_term]

        def fake_save_video(video_url, save_dir=""):
            downloaded_urls.append(video_url)
            return f"/tmp/{video_url.rsplit('/', 1)[-1]}"

        with (
            patch.dict(config.app, {"material_directory": ""}),
            patch.object(material, "search_videos_pexels", side_effect=fake_search),
            patch.object(material, "save_video", side_effect=fake_save_video),
        ):
            result = material.download_videos(
                task_id="ordered-materials",
                search_terms=["opening city", "middle office"],
                source="pexels",
                audio_duration=7,
                max_clip_duration=3,
                match_script_order=True,
            )

        self.assertEqual(
            downloaded_urls,
            [
                "https://v.example/a1.mp4",
                "https://v.example/b1.mp4",
                "https://v.example/a2.mp4",
            ],
        )
        self.assertEqual(result, ["/tmp/a1.mp4", "/tmp/b1.mp4", "/tmp/a2.mp4"])

    def test_download_videos_for_segments_selects_one_material_per_segment(self):
        """
        逐句匹配模式下，每个字幕 segment 只拿一个素材；如果候选里有
        未用过的 URL，优先使用未重复素材。完全搜不到时复用上一句素材。
        """
        shared = "https://v.example/shared.mp4"
        middle = "https://v.example/middle.mp4"
        search_results = {
            "opening city": [
                material.MaterialInfo(provider="pexels", url=shared, duration=5),
            ],
            "middle park": [
                material.MaterialInfo(provider="pexels", url=shared, duration=5),
                material.MaterialInfo(provider="pexels", url=middle, duration=5),
            ],
            "missing bridge": [],
        }
        downloaded_urls = []

        def fake_search(search_term, minimum_duration, video_aspect, **kwargs):
            return search_results[search_term]

        def fake_save_video(video_url, save_dir=""):
            downloaded_urls.append(video_url)
            return f"/tmp/{video_url.rsplit('/', 1)[-1]}"

        segments = [
            {"index": 1, "term": "opening city", "duration": 3, "material": ""},
            {"index": 2, "term": "middle park", "duration": 4, "material": ""},
            {"index": 3, "term": "missing bridge", "duration": 5, "material": ""},
        ]

        with (
            patch.dict(config.app, {"material_directory": ""}),
            patch.object(material, "search_videos_pexels", side_effect=fake_search),
            patch.object(material, "save_video", side_effect=fake_save_video),
        ):
            paths, matched_segments = material.download_videos_for_segments(
                task_id="segment-materials",
                segments=segments,
                source="pexels",
                max_clip_duration=3,
            )

        self.assertEqual(downloaded_urls, [shared, middle])
        self.assertEqual(paths, ["/tmp/shared.mp4", "/tmp/middle.mp4", "/tmp/middle.mp4"])
        self.assertEqual(matched_segments[0]["material"], "/tmp/shared.mp4")
        self.assertEqual(matched_segments[1]["material"], "/tmp/middle.mp4")
        self.assertEqual(matched_segments[2]["material"], "/tmp/middle.mp4")

    def test_download_candidate_videos_for_segments_keeps_three_remote_candidates_per_sentence(self):
        """
        编辑器候选准备阶段要为每句返回竖屏/横屏各 3 条远程候选，优先避免重复 URL；
        如果后续句子完全搜不到，则复用上一句候选并标记 fallback。
        """
        shared = "https://v.example/shared.mp4"
        search_results = {
            ("opening", "portrait"): [
                material.MaterialInfo(provider="pexels", url=shared, duration=6),
                material.MaterialInfo(provider="pexels", url="https://v.example/a2.mp4", duration=6),
                material.MaterialInfo(provider="pexels", url="https://v.example/a3.mp4", duration=6),
            ],
            ("opening", "landscape"): [
                material.MaterialInfo(provider="pexels", url="https://v.example/a4.mp4", duration=6),
                material.MaterialInfo(provider="pexels", url="https://v.example/a5.mp4", duration=6),
                material.MaterialInfo(provider="pexels", url="https://v.example/a6.mp4", duration=6),
            ],
            ("middle", "portrait"): [
                material.MaterialInfo(provider="pexels", url=shared, duration=6),
                material.MaterialInfo(provider="pexels", url="https://v.example/b2.mp4", duration=6),
                material.MaterialInfo(provider="pexels", url="https://v.example/b3.mp4", duration=6),
            ],
            ("middle", "landscape"): [
                material.MaterialInfo(provider="pexels", url="https://v.example/b4.mp4", duration=6),
                material.MaterialInfo(provider="pexels", url="https://v.example/b5.mp4", duration=6),
                material.MaterialInfo(provider="pexels", url="https://v.example/b6.mp4", duration=6),
            ],
            ("missing", "portrait"): [],
            ("missing", "landscape"): [],
        }
        def fake_search(search_term, minimum_duration, video_aspect, **kwargs):
            return search_results[(search_term, material.VideoAspect(video_aspect).name)]

        segments = [
            {"index": 1, "term": "opening", "duration": 3, "material": ""},
            {"index": 2, "term": "middle", "duration": 3, "material": ""},
            {"index": 3, "term": "missing", "duration": 3, "material": ""},
        ]

        with (
            patch.object(material, "search_videos_pexels", side_effect=fake_search),
            patch.object(material, "save_video") as save_video,
        ):
            paths, matched_segments = material.download_candidate_videos_for_segments(
                task_id="candidate-task",
                segments=segments,
                source="pexels",
                max_clip_duration=3,
            )

        self.assertEqual(paths, [])
        save_video.assert_not_called()
        self.assertEqual(len(matched_segments[0]["candidates"]), 6)
        self.assertEqual(len(matched_segments[1]["candidates"]), 6)
        self.assertEqual(len(matched_segments[2]["candidates"]), 6)
        self.assertEqual(matched_segments[0]["candidates"][0]["candidate_id"], "seg-1-cand-1")
        self.assertEqual(matched_segments[1]["candidates"][0]["source_url"], "https://v.example/b2.mp4")
        self.assertEqual(matched_segments[1]["candidates"][0]["preview_url"], "https://v.example/b2.mp4")
        self.assertEqual(matched_segments[0]["candidates"][0]["material"], "")
        self.assertEqual(matched_segments[0]["candidates"][0]["orientation"], "portrait")
        self.assertEqual(matched_segments[0]["candidates"][0]["orientation_label"], "竖屏")
        self.assertEqual(matched_segments[0]["candidates"][0]["group_rank"], 1)
        self.assertTrue(matched_segments[0]["candidates"][0]["is_default_group"])
        self.assertEqual(matched_segments[0]["candidates"][3]["orientation"], "landscape")
        self.assertFalse(matched_segments[0]["candidates"][3]["is_default_group"])
        self.assertIn("score", matched_segments[0]["candidates"][0])
        self.assertIn("quality_score", matched_segments[0]["candidates"][0])
        self.assertIn("relevance_score", matched_segments[0]["candidates"][0])
        self.assertIn("keyword_score", matched_segments[0]["candidates"][0])
        self.assertIn("visual_score", matched_segments[0]["candidates"][0])
        self.assertIn("reason", matched_segments[0]["candidates"][0])
        self.assertEqual(matched_segments[0]["material"], "")
        self.assertEqual(matched_segments[0]["preview_url"], shared)
        self.assertTrue(all(candidate["fallback"] for candidate in matched_segments[2]["candidates"]))

    def test_candidate_search_uses_first_ten_portrait_and_landscape_candidates(self):
        calls = []

        def fake_search(search_term, minimum_duration, video_aspect, **kwargs):
            orientation = material.VideoAspect(video_aspect).name
            calls.append((orientation, kwargs))
            self.assertFalse(kwargs["exact_resolution"])
            self.assertTrue(kwargs["use_orientation_filter"])
            self.assertEqual(kwargs["per_page"], 10)
            return [
                material.MaterialInfo(
                    provider="pexels",
                    url=f"https://v.example/{orientation}-{index}.mp4",
                    duration=6,
                    width=1080 if orientation == "portrait" else 1920,
                    height=1920 if orientation == "portrait" else 1080,
                    source_page_url=f"https://pexels.com/video/{orientation}-{index}",
                )
                for index in range(25)
            ]

        with patch.object(material, "search_videos_pexels", side_effect=fake_search):
            _, matched_segments = material.download_candidate_videos_for_segments(
                task_id="candidate-task",
                segments=[{"index": 1, "term": "Tiananmen Square", "duration": 3}],
                source="pexels",
                max_clip_duration=3,
            )

        self.assertEqual([call[0] for call in calls], ["portrait", "landscape"])
        candidates = matched_segments[0]["candidates"]
        self.assertEqual(len(candidates), 6)
        self.assertEqual(
            [candidate["orientation"] for candidate in candidates],
            ["portrait", "portrait", "portrait", "landscape", "landscape", "landscape"],
        )
        candidate_urls = {candidate["source_url"] for candidate in candidates}
        first_ten_urls = {
            f"https://v.example/{orientation}-{index}.mp4"
            for orientation in ("portrait", "landscape")
            for index in range(10)
        }
        self.assertTrue(candidate_urls.issubset(first_ten_urls))

    def test_rule_based_ranking_prefers_keyword_specific_metadata(self):
        search_results = {
            "portrait": [
                material.MaterialInfo(
                    provider="pexels",
                    url="https://v.example/generic-beijing-city.mp4",
                    duration=6,
                    width=1080,
                    height=1920,
                    source_page_url="https://www.pexels.com/video/beijing-city-skyline-1/",
                ),
                material.MaterialInfo(
                    provider="pexels",
                    url="https://v.example/tiananmen-square.mp4",
                    duration=6,
                    width=1080,
                    height=1920,
                    source_page_url="https://www.pexels.com/video/beijing-tiananmen-square-2/",
                ),
                material.MaterialInfo(
                    provider="pexels",
                    url="https://v.example/street-food.mp4",
                    duration=6,
                    width=1080,
                    height=1920,
                    source_page_url="https://www.pexels.com/video/beijing-street-market-3/",
                ),
            ],
            "landscape": [
                material.MaterialInfo(
                    provider="pexels",
                    url="https://v.example/generic-landscape.mp4",
                    duration=6,
                    width=1920,
                    height=1080,
                    source_page_url="https://www.pexels.com/video/beijing-skyline-4/",
                ),
                material.MaterialInfo(
                    provider="pexels",
                    url="https://v.example/tiananmen-landscape.mp4",
                    duration=6,
                    width=1920,
                    height=1080,
                    source_page_url="https://www.pexels.com/video/beijing-tiananmen-square-5/",
                ),
            ],
        }

        def fake_search(search_term, minimum_duration, video_aspect, **kwargs):
            return search_results[material.VideoAspect(video_aspect).name]

        with patch.object(material, "search_videos_pexels", side_effect=fake_search):
            _, matched_segments = material.download_candidate_videos_for_segments(
                task_id="candidate-task",
                segments=[
                    {
                        "index": 1,
                        "term": "Beijing Tiananmen Square",
                        "text": "Show Tiananmen Square in Beijing.",
                        "duration": 3,
                    }
                ],
                source="pexels",
                max_clip_duration=3,
            )

        candidates = matched_segments[0]["candidates"]
        self.assertEqual(candidates[0]["source_url"], "https://v.example/tiananmen-square.mp4")
        self.assertGreater(candidates[0]["keyword_score"], candidates[1]["keyword_score"])
        self.assertIn("tiananmen", candidates[0]["reason"])
        self.assertEqual(candidates[0]["orientation"], "portrait")
        self.assertTrue(candidates[0]["is_default_group"])
        self.assertEqual(candidates[3]["source_url"], "https://v.example/tiananmen-landscape.mp4")
        self.assertEqual(candidates[3]["orientation"], "landscape")
        self.assertFalse(candidates[3]["is_default_group"])

    def test_rule_based_ranking_prioritizes_place_over_visual_keyword(self):
        search_results = {
            "portrait": [
                material.MaterialInfo(
                    provider="pexels",
                    url="https://v.example/skylight-window.mp4",
                    duration=6,
                    width=1080,
                    height=1920,
                    source_page_url="https://www.pexels.com/video/skylight-window-roof-1/",
                ),
                material.MaterialInfo(
                    provider="pexels",
                    url="https://v.example/guangzhou-city.mp4",
                    duration=6,
                    width=1080,
                    height=1920,
                    source_page_url="https://www.pexels.com/video/guangzhou-city-skyline-2/",
                ),
            ],
            "landscape": [
                material.MaterialInfo(
                    provider="pexels",
                    url="https://v.example/skylight-landscape.mp4",
                    duration=6,
                    width=1920,
                    height=1080,
                    source_page_url="https://www.pexels.com/video/modern-skylight-building-3/",
                ),
                material.MaterialInfo(
                    provider="pexels",
                    url="https://v.example/canton-landscape.mp4",
                    duration=6,
                    width=1920,
                    height=1080,
                    source_page_url="https://www.pexels.com/video/canton-tower-guangzhou-4/",
                ),
            ],
        }

        def fake_search(search_term, minimum_duration, video_aspect, **kwargs):
            return search_results[material.VideoAspect(video_aspect).name]

        with patch.object(material, "search_videos_pexels", side_effect=fake_search):
            _, matched_segments = material.download_candidate_videos_for_segments(
                task_id="candidate-place-priority",
                segments=[
                    {
                        "index": 1,
                        "term": "Guangdong skylight",
                        "text": "广东城市天际线",
                        "duration": 3,
                    }
                ],
                source="pexels",
                max_clip_duration=3,
            )

        candidates = matched_segments[0]["candidates"]
        self.assertEqual(candidates[0]["source_url"], "https://v.example/guangzhou-city.mp4")
        self.assertGreater(candidates[0]["keyword_score"], candidates[1]["keyword_score"])
        self.assertIn("place match", candidates[0]["reason"])
        first_landscape = next(
            candidate for candidate in candidates if candidate["orientation"] == "landscape"
        )
        self.assertEqual(first_landscape["source_url"], "https://v.example/canton-landscape.mp4")

    def test_thumbnail_visual_score_handles_quality_range(self):
        normal = Image.effect_noise((160, 90), 55).convert("RGB")
        dark = Image.new("RGB", (160, 90), (2, 2, 2))
        bright = Image.new("RGB", (160, 90), (252, 252, 252))
        blurry = normal.filter(ImageFilter.GaussianBlur(radius=6))

        normal_score, normal_reason = material._score_thumbnail_image(normal)
        dark_score, dark_reason = material._score_thumbnail_image(dark)
        bright_score, bright_reason = material._score_thumbnail_image(bright)
        blurry_score, blurry_reason = material._score_thumbnail_image(blurry)

        self.assertGreater(normal_score, dark_score)
        self.assertGreater(normal_score, bright_score)
        self.assertGreater(normal_score, blurry_score)
        self.assertIn("dark", dark_reason)
        self.assertIn("bright", bright_reason)
        self.assertTrue(blurry_reason)
        self.assertTrue(normal_reason)

    def test_thumbnail_scoring_failure_falls_back_to_metadata(self):
        search_results = {
            ("Beijing Tiananmen Square", "portrait"): [
                material.MaterialInfo(
                    provider="pexels",
                    url="https://v.example/tiananmen-square.mp4",
                    duration=6,
                    width=1920,
                    height=1080,
                    thumbnail_url="https://images.example/missing.jpg",
                    source_page_url="https://www.pexels.com/video/beijing-tiananmen-square/",
                ),
                material.MaterialInfo(
                    provider="pexels",
                    url="https://v.example/generic.mp4",
                    duration=6,
                    width=1920,
                    height=1080,
                    source_page_url="https://www.pexels.com/video/city/",
                ),
            ],
            ("Beijing Tiananmen Square", "landscape"): [
                material.MaterialInfo(
                    provider="pexels",
                    url="https://v.example/tiananmen-landscape.mp4",
                    duration=6,
                    width=1920,
                    height=1080,
                    source_page_url="https://www.pexels.com/video/beijing-tiananmen-square-landscape/",
                ),
            ],
        }

        def fake_search(search_term, minimum_duration, video_aspect, **kwargs):
            return search_results[(search_term, material.VideoAspect(video_aspect).name)]

        with (
            patch.object(material, "search_videos_pexels", side_effect=fake_search),
            patch.object(material.requests, "get", side_effect=requests.Timeout("slow")),
        ):
            _, matched_segments = material.download_candidate_videos_for_segments(
                task_id="candidate-task",
                segments=[
                    {"index": 1, "term": "Beijing Tiananmen Square", "duration": 3}
                ],
                source="pexels",
                max_clip_duration=3,
            )

        candidates = matched_segments[0]["candidates"]
        self.assertEqual(candidates[0]["source_url"], "https://v.example/tiananmen-square.mp4")
        self.assertEqual(candidates[0]["visual_score"], 50.0)
        self.assertGreater(candidates[0]["score"], 0)

    def test_candidate_default_group_follows_output_aspect(self):
        def fake_search(search_term, minimum_duration, video_aspect, **kwargs):
            orientation = material.VideoAspect(video_aspect).name
            return [
                material.MaterialInfo(
                    provider="pexels",
                    url=f"https://v.example/{orientation}-{index}.mp4",
                    duration=6,
                    width=1080 if orientation == "portrait" else 1920,
                    height=1920 if orientation == "portrait" else 1080,
                    source_page_url=f"https://www.pexels.com/video/{orientation}-{index}/",
                )
                for index in range(3)
            ]

        cases = [
            (material.VideoAspect.portrait, "portrait"),
            (material.VideoAspect.landscape, "landscape"),
            (material.VideoAspect.square, "portrait"),
        ]
        for video_aspect, expected_default in cases:
            with self.subTest(video_aspect=video_aspect):
                with patch.object(material, "search_videos_pexels", side_effect=fake_search):
                    _, matched_segments = material.download_candidate_videos_for_segments(
                        task_id="candidate-default-group",
                        segments=[{"index": 1, "term": "city", "duration": 3}],
                        source="pexels",
                        video_aspect=video_aspect,
                        max_clip_duration=3,
                    )

                default_orientations = {
                    candidate["orientation"]
                    for candidate in matched_segments[0]["candidates"]
                    if candidate["is_default_group"]
                }
                self.assertEqual(default_orientations, {expected_default})
                self.assertEqual(
                    matched_segments[0]["candidates"][0]["orientation"],
                    expected_default,
                )

    def test_candidate_preparation_for_many_segments_does_not_call_codex(self):
        segments = [
            {"index": index + 1, "term": f"landmark {index}", "duration": 3}
            for index in range(20)
        ]

        def fake_search(search_term, minimum_duration, video_aspect, **kwargs):
            orientation = material.VideoAspect(video_aspect).name
            return [
                material.MaterialInfo(
                    provider="pexels",
                    url=f"https://v.example/{orientation}-{search_term.replace(' ', '-')}-{index}.mp4",
                    duration=6,
                    width=1080 if orientation == "portrait" else 1920,
                    height=1920 if orientation == "portrait" else 1080,
                    source_page_url=f"https://www.pexels.com/video/{orientation}-{search_term.replace(' ', '-')}-{index}/",
                )
                for index in range(10)
            ]

        with (
            patch.object(material, "search_videos_pexels", side_effect=fake_search),
            patch(
                "app.services.llm._generate_codex_response",
                side_effect=AssertionError("candidate ranking must not call Codex"),
            ) as codex_response,
            patch(
                "app.services.llm.subprocess.Popen",
                side_effect=AssertionError("candidate ranking must not spawn processes"),
            ),
        ):
            _, matched_segments = material.download_candidate_videos_for_segments(
                task_id="candidate-many-segments",
                segments=segments,
                source="pexels",
                max_clip_duration=3,
            )

        self.assertEqual(len(matched_segments), 20)
        self.assertEqual(
            sum(len(segment["candidates"]) for segment in matched_segments),
            120,
        )
        codex_response.assert_not_called()

    def test_x_candidates_are_cached_and_preserve_attribution(self):
        x_item = material.MaterialInfo(
            provider="x",
            url="https://video.twimg.com/guangzhou.mp4",
            duration=8,
            width=1280,
            height=720,
            source_page_url="https://x.com/citycam/status/123",
            author="citycam",
            tweet_id="123",
            media_type="video",
            attribution="X citycam",
        )

        with (
            patch.object(material, "search_videos_x", return_value=[x_item]) as search_x,
            patch.object(
                material,
                "save_video",
                return_value="/tmp/x-cache/guangzhou.mp4",
            ) as save_video,
        ):
            _, matched_segments = material.download_candidate_videos_for_segments(
                task_id="candidate-x-cache",
                segments=[
                    {
                        "index": 1,
                        "term": "Guangzhou skyline",
                        "duration": 3,
                        "providers": ["x"],
                    }
                ],
                source="pexels",
                max_clip_duration=3,
            )

        search_x.assert_called_once()
        save_video.assert_called_once()
        candidates = matched_segments[0]["candidates"]
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["provider"], "x")
        self.assertEqual(candidates[0]["material"], "/tmp/x-cache/guangzhou.mp4")
        self.assertEqual(candidates[0]["cached_path"], "/tmp/x-cache/guangzhou.mp4")
        self.assertEqual(candidates[0]["preview_url"], "/tmp/x-cache/guangzhou.mp4")
        self.assertEqual(candidates[0]["source_url"], "https://video.twimg.com/guangzhou.mp4")
        self.assertEqual(candidates[0]["source_page_url"], "https://x.com/citycam/status/123")
        self.assertEqual(candidates[0]["author"], "citycam")
        self.assertEqual(candidates[0]["tweet_id"], "123")
        self.assertEqual(candidates[0]["attribution"], "X citycam")


class TestCoverrProvider(unittest.TestCase):
    """
    Coverr 视频素材源(spec: 2026-06-09-coverr-video-provider-design.md)。
    全部用 unittest.mock 替换 requests，确保 CI 不依赖真实网络和真实 API key。
    """

    def setUp(self):
        self.original_app_config = dict(config.app)
        self.original_proxy_config = dict(config.proxy)

    def tearDown(self):
        config.app.clear()
        config.app.update(self.original_app_config)
        config.proxy.clear()
        config.proxy.update(self.original_proxy_config)

    # ---------------- Tests for search_videos_coverr ----------------

    def test_search_coverr_uses_mp4_download_url(self):
        """
        search_videos_coverr 应把每个 hit 转成 MaterialInfo，并把 urls.mp4_download
        直接作为 MaterialInfo.url。
        按 Coverr 官方文档 (api.coverr.co/docs/videos/#download-a-video),
        GET mp4_download 本身就被 Coverr 计入下载统计,无需额外 PATCH ping。
        同时验证 Authorization header 使用 Bearer scheme。
        """
        config.app["coverr_api_keys"] = ["coverr-key"]
        config.app.pop("tls_verify", None)
        config.proxy.clear()

        fake_response = SimpleNamespace(
            json=lambda: {
                "page": 0,
                "pages": 50,
                "page_size": 20,
                "total": 1,
                "hits": [
                    {
                        "id": "S1YbPl1NfI",
                        "duration": 11.625,
                        "aspect_ratio": "16:9",
                        "urls": {
                            "mp4": "https://storage.coverr.co/videos/abc?token=xyz",
                            "mp4_preview": "https://storage.coverr.co/videos/abc/preview?token=xyz",
                            "mp4_download": "https://storage.coverr.co/videos/abc/download?token=xyz",
                        },
                    }
                ],
            }
        )

        with patch(
            "app.services.material.requests.get", return_value=fake_response
        ) as get:
            results = material.search_videos_coverr("nature", minimum_duration=5)

        self.assertEqual(len(results), 1)
        item = results[0]
        self.assertEqual(item.provider, "coverr")
        self.assertEqual(item.duration, 11)
        # url 字段就是 mp4_download URL,不再做 coverr://id|url 编码
        self.assertEqual(
            item.url, "https://storage.coverr.co/videos/abc/download?token=xyz"
        )
        # Bearer auth + TLS verify on by default
        self.assertEqual(
            get.call_args.kwargs["headers"]["Authorization"], "Bearer coverr-key"
        )
        self.assertTrue(get.call_args.kwargs["verify"])

    def test_search_coverr_uses_tls_verification_by_default(self):
        """与 pexels/pixabay 一致:未显式配置时 TLS 校验默认开启。"""
        config.app["coverr_api_keys"] = ["coverr-key"]
        config.app.pop("tls_verify", None)
        config.proxy.clear()

        fake_response = SimpleNamespace(json=lambda: {"hits": []})

        with patch(
            "app.services.material.requests.get", return_value=fake_response
        ) as get:
            material.search_videos_coverr("nature", minimum_duration=1)

        self.assertTrue(get.call_args.kwargs["verify"])

    def test_search_coverr_allows_explicit_tls_disable_for_proxy(self):
        """企业自签证书代理场景必须能显式关闭 TLS 校验。"""
        config.app["coverr_api_keys"] = ["coverr-key"]
        config.app["tls_verify"] = False
        config.proxy.clear()

        fake_response = SimpleNamespace(json=lambda: {"hits": []})

        with patch(
            "app.services.material.requests.get", return_value=fake_response
        ) as get:
            material.search_videos_coverr("nature", minimum_duration=1)

        self.assertFalse(get.call_args.kwargs["verify"])

    def test_search_coverr_filters_by_min_duration_and_accepts_string(self):
        """
        Coverr duration 字段在不同响应里可能是 number 或 string,
        两种格式都要接受;低于 minimum_duration 的应被过滤。
        """
        config.app["coverr_api_keys"] = ["coverr-key"]
        config.app.pop("tls_verify", None)
        config.proxy.clear()

        fake_response = SimpleNamespace(
            json=lambda: {
                "hits": [
                    {
                        "id": "shortvid",
                        "duration": 3,  # below minimum
                        "urls": {"mp4_download": "https://example.com/a.mp4"},
                    },
                    {
                        "id": "stringdur",
                        "duration": "10.500000",  # string accepted
                        "urls": {"mp4_download": "https://example.com/b.mp4"},
                    },
                ]
            }
        )

        with patch(
            "app.services.material.requests.get", return_value=fake_response
        ):
            results = material.search_videos_coverr("x", minimum_duration=5)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].duration, 10)
        self.assertEqual(results[0].url, "https://example.com/b.mp4")

    def test_search_coverr_skips_invalid_items(self):
        """缺 id 或缺 urls.mp4_download 的条目应被跳过,不应抛异常。"""
        config.app["coverr_api_keys"] = ["coverr-key"]
        config.app.pop("tls_verify", None)
        config.proxy.clear()

        fake_response = SimpleNamespace(
            json=lambda: {
                "hits": [
                    {  # missing urls.mp4_download
                        "id": "no-download",
                        "duration": 10,
                        "urls": {"mp4_preview": "https://example.com/preview.mp4"},
                    },
                    {  # missing id
                        "duration": 10,
                        "urls": {"mp4_download": "https://example.com/x.mp4"},
                    },
                    {  # valid baseline
                        "id": "good",
                        "duration": 10,
                        "urls": {"mp4_download": "https://example.com/good.mp4"},
                    },
                ]
            }
        )

        with patch(
            "app.services.material.requests.get", return_value=fake_response
        ):
            results = material.search_videos_coverr("x", minimum_duration=1)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://example.com/good.mp4")

    def test_search_coverr_returns_empty_on_failure(self):
        """
        响应结构异常 / 网络异常时,函数必须返回 [] 而不是抛异常,
        与 pexels/pixabay 行为保持一致。
        """
        config.app["coverr_api_keys"] = ["coverr-key"]
        config.app.pop("tls_verify", None)
        config.proxy.clear()

        # Subtest A: malformed response (no "hits" key)
        with self.subTest("malformed response"):
            fake_response = SimpleNamespace(
                json=lambda: {"error": "rate limited"}
            )
            with patch(
                "app.services.material.requests.get", return_value=fake_response
            ):
                results = material.search_videos_coverr("x", minimum_duration=1)
            self.assertEqual(results, [])

        # Subtest B: network exception bubbles up from requests.get
        with self.subTest("network exception"):
            with patch(
                "app.services.material.requests.get",
                side_effect=requests.ConnectionError("boom"),
            ):
                results = material.search_videos_coverr("x", minimum_duration=1)
            self.assertEqual(results, [])

    # ---------------- Tests for download_videos coverr branch ----------------

    def test_download_videos_passes_mp4_download_url_to_save_video(self):
        """
        在 source="coverr" 时:
          1. dispatch 到 search_videos_coverr
          2. coverr item 走通用下载路径:save_video 收到的就是 mp4_download URL
             (不再有 coverr://id|url 编码,也不再调用 PATCH ping)
          3. 返回保存路径
        """
        config.app["coverr_api_keys"] = ["coverr-key"]
        config.app.pop("tls_verify", None)
        config.app.pop("material_directory", None)
        config.proxy.clear()

        fake_item = material.MaterialInfo()
        fake_item.provider = "coverr"
        fake_item.url = "https://storage.coverr.co/videos/abc/download?token=xyz"
        fake_item.duration = 10

        with patch(
            "app.services.material.search_videos_coverr",
            return_value=[fake_item],
        ) as search, patch(
            "app.services.material.save_video",
            return_value="/tmp/coverr-saved.mp4",
        ) as save:
            result = material.download_videos(
                task_id="t-coverr",
                search_terms=["nature"],
                source="coverr",
                audio_duration=5,
                max_clip_duration=5,
            )

        # 1. dispatch
        self.assertEqual(search.call_count, 1)

        # 2. save_video 收到的就是 mp4_download URL,原样传入
        save_url = save.call_args.kwargs.get("video_url") or save.call_args.args[0]
        self.assertEqual(
            save_url, "https://storage.coverr.co/videos/abc/download?token=xyz"
        )

        # 3. 返回值正确
        self.assertEqual(result, ["/tmp/coverr-saved.mp4"])


if __name__ == "__main__":
    unittest.main()
