import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.config import config
from app.services import x_material


def _completed(args, stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(
        args=args,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


class TestXMaterialAdapter(unittest.TestCase):
    def setUp(self):
        self.original_app_config = dict(config.app)

    def tearDown(self):
        config.app.clear()
        config.app.update(self.original_app_config)

    def test_parse_nested_tweet_video_media(self):
        payload = {
            "tweets": [
                {
                    "id_str": "123",
                    "username": "citycam",
                    "url": "https://x.com/citycam/status/123",
                    "text": "Guangzhou skyline",
                    "media": [
                        {
                            "type": "video",
                            "preview_image_url": "https://pbs.twimg.com/thumb.jpg",
                            "video_info": {
                                "duration_millis": 6200,
                                "variants": [
                                    {
                                        "content_type": "application/x-mpegURL",
                                        "url": "https://video.twimg.com/a.m3u8",
                                    },
                                    {
                                        "content_type": "video/mp4",
                                        "bitrate": 832000,
                                        "url": "https://video.twimg.com/a.mp4",
                                    },
                                ],
                            },
                            "original_info": {"width": 1280, "height": 720},
                        }
                    ],
                }
            ]
        }

        items = x_material.parse_x_media_payload(payload)

        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item.provider, "x")
        self.assertEqual(item.url, "https://video.twimg.com/a.mp4")
        self.assertEqual(item.duration, 6.2)
        self.assertEqual(item.width, 1280)
        self.assertEqual(item.height, 720)
        self.assertEqual(item.thumbnail_url, "https://pbs.twimg.com/thumb.jpg")
        self.assertEqual(item.source_page_url, "https://x.com/citycam/status/123")
        self.assertEqual(item.author, "citycam")
        self.assertEqual(item.tweet_id, "123")
        self.assertEqual(item.media_type, "video")
        self.assertEqual(item.attribution, "X citycam")

    def test_search_x_videos_uses_agentreach_backend_and_filters_media(self):
        config.app["enable_x_materials"] = True
        config.app["x_search_limit"] = 10
        doctor_payload = {
            "twitter": {
                "status": "ok",
                "active_backend": "twitter-cli",
            }
        }
        search_payload = {
            "tweets": [
                {
                    "id": "456",
                    "username": "news",
                    "url": "https://x.com/news/status/456",
                    "media": [
                        {
                            "type": "video",
                            "video_url": "https://video.twimg.com/news.mp4",
                            "duration": 8,
                        },
                        {
                            "type": "photo",
                            "media_url_https": "https://pbs.twimg.com/image.jpg",
                        },
                    ],
                }
            ]
        }
        calls = []

        def fake_run(args, timeout=None, cwd=None):
            calls.append(args)
            if args[-2:] == ["doctor", "--json"]:
                return _completed(args, stdout=json.dumps(doctor_payload))
            return _completed(args, stdout=json.dumps(search_payload))

        with patch.object(x_material, "run_command", side_effect=fake_run):
            items = x_material.search_x_videos("Guangzhou skyline", minimum_duration=2)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].url, "https://video.twimg.com/news.mp4")
        self.assertEqual(calls[0][-2:], ["doctor", "--json"])
        self.assertEqual(calls[1][:2], ["twitter", "search"])

    def test_search_x_videos_returns_empty_when_agentreach_unavailable(self):
        config.app["enable_x_materials"] = True
        doctor_payload = {
            "twitter": {
                "status": "warn",
                "active_backend": "twitter-cli",
            }
        }

        with patch.object(
            x_material,
            "run_command",
            return_value=_completed(["agent-reach"], stdout=json.dumps(doctor_payload)),
        ):
            items = x_material.search_x_videos("anything", minimum_duration=1)

        self.assertEqual(items, [])


if __name__ == "__main__":
    unittest.main()
