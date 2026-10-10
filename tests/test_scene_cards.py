from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path

from kk_mod_helper.gui import _items_for_selected_cards, _png_preview_data, _scene_cards_from_report
from kk_mod_helper.reporting import build_missing_report


class SceneCardReportTests(unittest.TestCase):
    def test_report_keeps_all_scene_dependencies(self) -> None:
        card = r"D:\Games\Koikatu\UserData\Studio\scene\example.png"
        requirements = {
            "missing.mod": {
                "guid": "missing.mod",
                "name": "Missing Mod",
                "author": "Author A",
                "website": "",
                "cards": [{"path": card, "filename": "example.png", "card_type": "scene"}],
            },
            "installed.mod": {
                "guid": "installed.mod",
                "name": "Installed Mod",
                "author": "Author B",
                "website": "",
                "cards": [{"path": card, "filename": "example.png", "card_type": "scene"}],
            },
        }
        installed = {
            "installed.mod": {
                "guid": "installed.mod",
                "name": "Installed Mod",
                "path": r"D:\Games\Koikatu\mods\installed.zipmod",
                "file_size": 123,
            }
        }

        report = build_missing_report(Path(r"D:\Games\Koikatu"), requirements, installed, {})

        self.assertEqual(report["schema_version"], 2)
        self.assertEqual([item["guid"] for item in report["items"]], ["missing.mod"])
        self.assertEqual(len(report["cards"]), 1)
        self.assertEqual(report["cards"][0]["mod_count"], 2)
        self.assertEqual(report["cards"][0]["missing_count"], 1)
        states = {item["guid"]: item["installed"] for item in report["cards"][0]["mods"]}
        self.assertEqual(states, {"installed.mod": True, "missing.mod": False})

    def test_selected_cards_show_installed_and_missing_union(self) -> None:
        missing = {
            "missing.mod": {
                "guid": "missing.mod",
                "status": "downloadable",
                "name": "Missing Mod",
                "cards": [
                    {"path": "card-a.png", "filename": "card-a.png", "card_type": "scene"},
                    {"path": "card-b.png", "filename": "card-b.png", "card_type": "scene"},
                ],
            }
        }
        cards = {
            "card-a.png": {
                "path": "card-a.png",
                "filename": "card-a.png",
                "mods": [
                    {"guid": "missing.mod", "installed": False},
                    {"guid": "installed.mod", "name": "Installed Mod", "installed": True},
                ],
            },
            "card-b.png": {
                "path": "card-b.png",
                "filename": "card-b.png",
                "mods": [{"guid": "missing.mod", "installed": False}],
            },
        }

        visible = _items_for_selected_cards(missing, cards, {"card-a.png", "card-b.png"})

        self.assertEqual(set(visible), {"missing.mod", "installed.mod"})
        self.assertEqual(visible["installed.mod"]["status"], "satisfied")
        self.assertEqual(len(visible["missing.mod"]["cards"]), 2)

    def test_old_report_derives_missing_only_scene_index(self) -> None:
        report = {
            "items": [
                {
                    "guid": "missing.mod",
                    "cards": [
                        {"path": "scene.png", "filename": "scene.png", "card_type": "scene"},
                        {"path": "chara.png", "filename": "chara.png", "card_type": "chara"},
                    ],
                }
            ]
        }
        cards = _scene_cards_from_report(report)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["path"], "scene.png")
        self.assertEqual(cards[0]["missing_count"], 1)

    def test_png_preview_ignores_appended_card_data(self) -> None:
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scene.png"
            path.write_bytes(png + b"KOIKATU-APPENDED-DATA")
            preview = base64.b64decode(_png_preview_data(path))
        self.assertTrue(preview.startswith(b"\x89PNG"))
        self.assertTrue(preview.endswith(b"IEND\xaeB`\x82"))
        self.assertNotIn(b"KOIKATU", preview)


if __name__ == "__main__":
    unittest.main()
