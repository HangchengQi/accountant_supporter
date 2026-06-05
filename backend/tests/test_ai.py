import os
import unittest
from unittest.mock import patch

from app.ai import (
    ClassificationResult,
    OpenAIClassifier,
    OpenAIConfig,
    OpenAIProcessor,
    ai_status,
    classify_email,
    create_ai_processor,
    LocalHeuristicProcessor,
)
from app.schemas import EmailSampleIn
from app.workflow import Workflow


class CapturingOpenAIProcessor(OpenAIProcessor):
    def __init__(self, config: OpenAIConfig) -> None:
        super().__init__(config)
        self.payload = {}

    def _post_response(self, payload: dict) -> dict:
        self.payload = payload
        return {
            "output_text": (
                '{"summary":"Captured","category":"invoice","vendor_name":null,'
                '"invoice_number":null,"invoice_date":null,"due_date":null,'
                '"amount":null,"currency":null,"confidence":0.75,"needs_review":true}'
            )
        }


class CapturingOpenAIClassifier(OpenAIClassifier):
    def __init__(self, config: OpenAIConfig) -> None:
        super().__init__(config)
        self.payload = {}

    def _post_response(self, payload: dict) -> dict:
        self.payload = payload
        return {
            "output_text": (
                '{"category":"bookkeeping_question","confidence":0.7,"needs_review":true}'
            )
        }


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

    def test_status_reports_per_job_models(self) -> None:
        status = ai_status(
            {
                "provider": "openai",
                "openai_api_key": "saved-key",
                "openai_model": "strong-model",
                "openai_classification_model": "simple-model",
            }
        )

        self.assertEqual(status["job_models"]["classify_email"], "simple-model")
        self.assertEqual(status["job_models"]["process_email"], "strong-model")

    def test_openai_processor_uses_workflow_ai_instructions(self) -> None:
        workflow = Workflow(
            name="vendor_invoice_email",
            version=1,
            summary_sentences=3,
            require_human_review=True,
            minimum_confidence_for_auto_upload=0.9,
            zoho_mode="dry_run",
            ai_instructions="Follow the private workflow process.",
            raw={},
        )
        email = EmailSampleIn(subject="Invoice", sender="Vendor <ap@example.com>", body="Amount due $10.00")
        processor = CapturingOpenAIProcessor(OpenAIConfig(api_key="key", model="model"))

        processor.process(email, workflow)

        self.assertEqual(processor.payload["instructions"], "Follow the private workflow process.")

    def test_openai_classifier_uses_classification_model(self) -> None:
        email = EmailSampleIn(subject="Question", sender="Vendor <ap@example.com>", body="Is this tax related?")
        classifier = CapturingOpenAIClassifier(OpenAIConfig(api_key="key", model="simple-model"))

        result = classifier.classify(email)

        self.assertEqual(classifier.payload["model"], "simple-model")
        self.assertEqual(result.category, "bookkeeping_question")
        self.assertEqual(result.mode, "openai:simple-model")

    def test_uncertain_classification_uses_openai_when_configured(self) -> None:
        email = EmailSampleIn(subject="Question", sender="Vendor <ap@example.com>", body="Can you check this tax form?")
        settings = {
            "provider": "openai",
            "openai_api_key": "saved-key",
            "openai_model": "strong-model",
            "openai_classification_model": "simple-model",
        }

        with patch("app.ai.OpenAIClassifier") as classifier_class:
            classifier_class.return_value.classify.return_value = ClassificationResult(
                "bookkeeping_question",
                0.7,
                True,
                "openai:simple-model",
            )
            result = classify_email(email, settings)

        self.assertEqual(result.mode, "openai:simple-model")
        classifier_class.assert_called_once()
        self.assertEqual(classifier_class.call_args.args[0].model, "simple-model")


if __name__ == "__main__":
    unittest.main()
