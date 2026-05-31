import os
import unittest
from unittest.mock import patch

from app.ai import (
    OpenAIConfig,
    OpenAIProcessor,
    ai_status,
    create_ai_processor,
    LocalHeuristicProcessor,
)


class AIConfigTest(unittest.TestCase):
    def test_openai_config_from_env(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "test-key",
                "OPENAI_MODEL": "test-model",
            },
            clear=False,
        ):
            config = OpenAIConfig.from_env()

        self.assertTrue(config.is_configured)
        self.assertEqual(config.model, "test-model")

    def test_local_fallback_when_openai_key_missing(self) -> None:
        with patch.dict(os.environ, {"AI_PROVIDER": "openai", "OPENAI_API_KEY": ""}, clear=False):
            processor = create_ai_processor()
            status = ai_status()

        self.assertIsInstance(processor, LocalHeuristicProcessor)
        self.assertEqual(status["active_provider"], "local")

    def test_saved_settings_enable_openai_processor(self) -> None:
        settings = {
            "provider": "openai",
            "openai_api_key": "saved-key",
            "openai_model": "saved-model",
        }

        processor = create_ai_processor(settings)
        status = ai_status(settings)

        self.assertIsInstance(processor, OpenAIProcessor)
        self.assertEqual(status["active_provider"], "openai")
        self.assertEqual(status["model"], "saved-model")
        self.assertTrue(status["settings"]["has_openai_api_key"])


if __name__ == "__main__":
    unittest.main()
