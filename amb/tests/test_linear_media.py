from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

STAGED = Path(__file__).resolve().parents[1] / "linear-app"
PATH = STAGED if STAGED.is_file() else Path.home() / "bin" / "linear-app"
loader = SourceFileLoader("linear_app_media", str(PATH))
app = module_from_spec(spec_from_loader(loader.name, loader))
loader.exec_module(app)
U = "https://uploads.linear.app/w/a/b"


class Media(unittest.TestCase):
    def test_paths_in_result_and_dirs_are_collected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "v").mkdir()
            (root / "v" / "a.mp4").write_bytes(b"x")
            (root / "v" / "notes.txt").write_text("x")
            (root / "t.csv").write_text("a,b\n1,2\n")
            (root / "result.md").write_text("x")
            with patch.object(app, "LOCAL_PATH", app.re.compile(app.re.escape(d) + r"/[^\s`，。]+")):
                found = app.mentioned_media(root / "result.md", f"见 `{d}/v/`，表格 {d}/t.csv。")
        self.assertEqual([p.name for p in found], ["a.mp4", "t.csv"])

    def test_entries(self):
        with tempfile.TemporaryDirectory() as d:
            csv = Path(d) / "r.csv"
            csv.write_text("run,sr\nA,0.9\n")
            self.assertIn("| run | sr |", app.media_entry(csv, U))
            self.assertEqual(app.media_entry(Path(d) / "a.png", U), f"![a.png]({U})")
            self.assertEqual(app.media_entry(Path(d) / "a.mp4", U), f"[a.mp4]({U})")

    def test_readback_tolerates_video_reserialization(self):
        expected = f"x\n\n[a.mp4]({U})\n"
        card = {"description": f"x\n\n[uuid-1]({U})\n", "updatedAt": "t"}
        with patch.object(app, "issue", return_value=card):
            app.verify("<REDACTED_ID>", expected)


if __name__ == "__main__":
    unittest.main()
