import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.main import app_update_status, pull_latest_app_update


class AppUpdateTest(unittest.TestCase):
    def test_update_status_marks_non_git_install_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch("app.main.get_repo_root", return_value=Path(directory)):
                status = app_update_status()

        self.assertFalse(status["is_git_repo"])
        self.assertEqual(status["commit"], "")
        self.assertEqual(status["branch"], "")

    def test_pull_latest_update_reports_changed_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".git").mkdir()
            calls = []

            def fake_git(args, timeout):
                calls.append(args)
                if args == ["rev-parse", "--short", "HEAD"] and len(calls) == 1:
                    return {"stdout": "old123\n", "stderr": "", "returncode": 0}
                if args == ["rev-parse", "--short", "HEAD"]:
                    return {"stdout": "new456\n", "stderr": "", "returncode": 0}
                if args == ["branch", "--show-current"]:
                    return {"stdout": "main\n", "stderr": "", "returncode": 0}
                if args == ["pull", "--ff-only"]:
                    return {"stdout": "Fast-forward\n", "stderr": "", "returncode": 0}
                raise AssertionError(args)

            with (
                patch("app.main.get_repo_root", return_value=root),
                patch("app.main._run_git_command", side_effect=fake_git),
            ):
                result = pull_latest_app_update()

        self.assertTrue(result["updated"])
        self.assertEqual(result["before_commit"], "old123")
        self.assertEqual(result["after_commit"], "new456")
        self.assertTrue(result["restart_recommended"])


if __name__ == "__main__":
    unittest.main()
