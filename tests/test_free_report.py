"""Check that a free report shows every video it marks as sent."""

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


APP = Path(__file__).resolve().parents[1] / "app.py"


class FreeReportTest(unittest.TestCase):
    def test_every_readable_video_is_reported_before_it_is_retired(self):
        with tempfile.TemporaryDirectory() as data_dir:
            with patch.dict(os.environ, {"MORNING_DATA_DIR": data_dir,
                                      "MORNING_MODEL_URL": "", "MORNING_MODEL": ""}):
                spec = importlib.util.spec_from_file_location("leave_youtube_under_test", APP)
                app = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(app)

            def video(id_, title):
                return {"id": id_, "title": title, "channel": "Example Reviews",
                        "channel_id": "example-reviews"}

            candidates = [video("alpha", "Alpha phone battery test"),
                          video("beta", "Beta phone camera test"),
                          video("unreadable", "Unreadable phone review")]
            alternate = video("delta", "Delta phone price test")
            speech = {
                "alpha": "The Alpha phone battery lasted two days in our test, compared with one day on the older model.",
                "beta": "The Beta phone camera kept the night sky clear because its longer exposure gathered more light.",
                "unreadable": "Welcome back everyone, please subscribe and like this video.",
                "delta": "The Delta phone price rose by twenty percent, but the base storage stayed at the same size.",
            }
            config = {"subjects": [], "timezone": "Europe/Dublin", "angle": ""}
            group = {"subject": "Phones", "requested": 3,
                     "videos": candidates, "alternates": [alternate]}

            with patch.object(app, "settings", return_value=config), \
                 patch.object(app, "build_selection", return_value=[group]), \
                 patch.object(app, "transcript", side_effect=lambda id_: speech[id_]), \
                 patch.object(app, "send_telegram", return_value=[123]) as send:
                result = app.run("send")

            message = send.call_args.args[0]
            for id_ in ("alpha", "beta", "delta"):
                self.assertIn(speech[id_], message)
                self.assertIn(id_, app.sent_video_ids())
            self.assertNotIn("Unreadable phone review", message)
            self.assertNotIn("unreadable", app.sent_video_ids())
            self.assertEqual(result["used_count"], 3)
            self.assertEqual(result["selection"], [])


if __name__ == "__main__":
    unittest.main()
