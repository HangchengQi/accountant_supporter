import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.main import app_update_status, page_shell, sync_latest_app_update


class AppUpdateTest(unittest.TestCase):
    def test_page_shell_includes_shutdown_control(self) -> None:
        html = page_shell("Test", "<main></main>")

        self.assertIn('id="shutdown-app-button"', html)
        self.assertIn("/api/app/shutdown", html)

    def test_update_status_marks_non_git_install_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch("app.main.get_repo_root", return_value=Path(directory)):
                status = app_update_status()

        self.assertTrue(status["updates_enabled"])
        self.assertFalse(status["is_git_repo"])
        self.assertEqual(status["commit"], "")
        self.assertEqual(status["branch"], "")

    def test_update_status_respects_disabled_updates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".git").mkdir()
            with (
                patch("app.main.get_repo_root", return_value=root),
                patch.dict("os.environ", {"APP_UPDATES_ENABLED": "false"}),
                patch("app.main._run_git_command") as git_command,
            ):
                status = app_update_status()

        self.assertFalse(status["updates_enabled"])
        self.assertTrue(status["is_git_repo"])
        self.assertFalse(status["update_available"])
        git_command.assert_not_called()

    def test_sync_latest_update_rejects_disabled_updates(self) -> None:
        with patch.dict("os.environ", {"APP_UPDATES_ENABLED": "false"}):
            with self.assertRaisesRegex(ValueError, "disabled"):
                sync_latest_app_update()

    def test_update_status_reports_available_update(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".git").mkdir()

            def fake_git(args, timeout):
                if args == ["rev-parse", "--short", "HEAD"]:
                    return {"stdout": "old123\n", "stderr": "", "returncode": 0}
                if args == ["branch", "--show-current"]:
                    return {"stdout": "main\n", "stderr": "", "returncode": 0}
                if args == ["fetch", "--quiet"]:
                    return {"stdout": "", "stderr": "", "returncode": 0}
                if args == ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]:
                    return {"stdout": "origin/main\n", "stderr": "", "returncode": 0}
                if args == ["rev-parse", "--short", "origin/main"]:
                    return {"stdout": "new456\n", "stderr": "", "returncode": 0}
                if args == ["rev-list", "--count", "HEAD..origin/main"]:
                    return {"stdout": "2\n", "stderr": "", "returncode": 0}
                raise AssertionError(args)

            with (
                patch("app.main.get_repo_root", return_value=root),
                patch("app.main._run_git_command", side_effect=fake_git),
            ):
                status = app_update_status()

        self.assertTrue(status["update_available"])
        self.assertEqual(status["behind_count"], 2)
        self.assertEqual(status["remote_commit"], "new456")

    def test_sync_latest_update_reports_changed_commit(self) -> None:
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
                result = sync_latest_app_update()

        self.assertTrue(result["updated"])
        self.assertEqual(result["before_commit"], "old123")
        self.assertEqual(result["after_commit"], "new456")
        self.assertTrue(result["restart_recommended"])


if __name__ == "__main__":
    unittest.main()
