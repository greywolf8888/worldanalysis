"""Synthetic CLI integration: local packages -> composed Bundle -> Vault."""
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

from PIL import Image


class MaterialsObsidianIntegrationTests(unittest.TestCase):
    def test_two_local_packages_compose_to_readable_vault_without_media(self):
        with tempfile.TemporaryDirectory(prefix="worldanalysis-intake-vault-") as temporary:
            base = Path(temporary)
            root, vault = base / "workspace", base / "vault"
            root.mkdir()
            checkout = Path(__file__).resolve().parents[1]

            def cli(module, *arguments):
                result = subprocess.run(
                    [sys.executable, "-B", "-m", module, *map(str, arguments)],
                    cwd=checkout, capture_output=True, text=True, timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                return json.loads(result.stdout)

            cli("worldanalysis", "--root", root, "create-project",
                "--project-id", "synthetic-course", "--topic", "合成联动课程")
            bundles = []
            for number in (1, 2):
                package = root / "手动投放" / ("lesson" + str(number))
                package.mkdir(parents=True)
                Image.new("RGB", (32, 20), "blue").save(package / "image.png")
                (package / "note.md").write_text(
                    "平台笔记（合成），不等于全文。\n![本地图](image.png)\n",
                    encoding="utf-8",
                )
                manifest = {
                    "schema_version": "1.0", "material_id": "lesson-" + str(number),
                    "title": "合成第" + str(number) + "课",
                    "source": {"provider": "baidu", "file_id": "synthetic-" + str(number),
                               "path": "/apps/教育分析学习/合成课程/课" + str(number) + ".mp4",
                               "duration_ms": 20000},
                    "note": {"path": "note.md"},
                    "images": [{"path": "image.png", "source_kind": "platform_note_image"}],
                }
                if number == 1:
                    (package / "text.srt").write_text(
                        "1\n00:00:01,000 --> 00:00:03,000\n构造字幕开头。\n\n"
                        "2\n00:00:08,000 --> 00:00:10,000\n构造字幕结尾。\n",
                        encoding="utf-8",
                    )
                    manifest["transcript"] = {
                        "path": "text.srt", "kind": "timed_subtitle", "origin": "asr",
                        "source_method": "synthetic_fixture_not_online",
                    }
                path = package / "manifest.json"
                path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
                imported = cli("worldanalysis.platform_materials", "--root", root,
                               "--project", "synthetic-course", "--manifest", path)
                bundles.append(imported["bundle_id"])
                repeated = cli("worldanalysis.platform_materials", "--root", root,
                               "--project", "synthetic-course", "--manifest", path)
                self.assertEqual(imported["bundle_id"], repeated["bundle_id"])

            composed = cli("worldanalysis", "--root", root, "compose", "--project", "synthetic-course",
                           "--bundle", bundles[0], "--bundle", bundles[1])
            self.assertEqual(composed["coverage"]["claimed_status"], "partial")
            result = cli("worldanalysis.obsidian", "--root", root, "--project", "synthetic-course",
                         "--vault", vault, "--bundle", composed["bundle_id"])
            self.assertEqual((result["asset_count"], result["image_count"], result["image_occurrence_count"]), (2, 1, 2))
            self.assertEqual(result["source_coverage_status"], "partial")
            pages = list((Path(result["course_path"]) / "资料").glob("*/资料.md"))
            self.assertEqual(len(pages), 2)
            page_text = "\n".join(page.read_text(encoding="utf-8") for page in pages)
            for expected in ("合成第1课", "合成第2课", "构造字幕开头。", "构造字幕结尾。",
                             "平台 AI 笔记", "语音识别文字", "declared_unverified"):
                self.assertIn(expected, page_text)
            for page in [*pages, Path(result["overview_path"])]:
                for target in re.findall(r"(?<!\\)\]\(([^)]+)\)", page.read_text(encoding="utf-8")):
                    self.assertTrue((page.parent / target).resolve().is_file(), target)
            personal = pages[0].with_name("我的笔记.md")
            personal.write_text("我的整理保留。", encoding="utf-8")
            repeated = cli("worldanalysis.obsidian", "--root", root, "--project", "synthetic-course",
                           "--vault", vault, "--bundle", composed["bundle_id"])
            self.assertEqual(repeated["created_files"], [])
            self.assertEqual(repeated["updated_files"], [])
            self.assertEqual(personal.read_text(encoding="utf-8"), "我的整理保留。")
            self.assertFalse(any(path.suffix.lower() in {".mp4", ".mkv", ".wav", ".mp3", ".m4a"}
                                 for path in base.rglob("*") if path.is_file()))


if __name__ == "__main__":
    unittest.main()
