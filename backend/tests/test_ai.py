import os
import unittest
from unittest.mock import patch

from app.ai import OpenAIConfig, ai_status, create_ai_processor, LocalHeuristicProcessor


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


if __name__ == "__main__":
    unittest.main()
