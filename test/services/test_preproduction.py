import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.services import preproduction


class TestPreproductionPlan(unittest.TestCase):
    def test_parse_csv_normalizes_chinese_keywords_and_providers(self):
        rows = preproduction.parse_preproduction_csv(
            "segment_index,title,script,keyword_cn,material_query,providers,source_urls\n"
            "1,城市视频,广州GDP增长。,广州CBD天际线,Guangzhou CBD skyline,\"x, pexels\",https://x.com/a/status/1\n"
            "2,城市视频,北京历史地标。,北京天安门,Beijing Tiananmen Square,pexels,\n"
        )

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["keyword_cn"], "广州CBD天际线")
        self.assertEqual(rows[0]["material_query"], "Guangzhou CBD skyline")
        self.assertEqual(rows[0]["providers"], ["x", "pexels"])
        self.assertEqual(rows[0]["source_urls"], ["https://x.com/a/status/1"])

        plan = preproduction.build_preproduction_plan(rows)
        self.assertEqual(plan["title"], "城市视频")
        self.assertEqual(
            plan["script"],
            "广州GDP增长。\n北京历史地标。",
        )
        self.assertEqual(
            plan["display_terms"],
            ["广州CBD天际线", "北京天安门"],
        )
        self.assertEqual(
            plan["search_terms"],
            ["Guangzhou CBD skyline", "Beijing Tiananmen Square"],
        )
        self.assertTrue(plan["signature"])

    def test_source_url_without_provider_defaults_to_url_provider(self):
        rows = preproduction.parse_preproduction_csv(
            "script,keyword_cn,material_query,source_urls\n"
            "Use supplied clip.,测试直链,test direct,https://cdn.example.com/a.mp4\n"
        )

        self.assertEqual(rows[0]["providers"], ["url"])
        self.assertEqual(rows[0]["source_urls"], ["https://cdn.example.com/a.mp4"])


if __name__ == "__main__":
    unittest.main()
