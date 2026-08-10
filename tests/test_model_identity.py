from __future__ import annotations

import unittest

from agentlog.normalize.model_identity import (
    UNKNOWN_MODEL_LABEL,
    display_model,
    is_known_non_model,
    resolve_model_identity,
)
from agentlog.registry.models import AGENT_PROFILES, MODELS_BY_ID, PROVIDERS


class ModelIdentityResolveTests(unittest.TestCase):
    def test_alias_resolution(self) -> None:
        ident = resolve_model_identity("cursor-grok-4.5-high-fast")
        self.assertEqual(ident.canonical, "grok-4.5")
        self.assertEqual(ident.provider, "xai")
        self.assertEqual(ident.raw, "cursor-grok-4.5-high-fast")
        self.assertIsNone(ident.agent_profile)

    def test_effort_suffix_not_part_of_identity(self) -> None:
        ident = resolve_model_identity("gpt-5.6-sol-high")
        self.assertEqual(ident.canonical, "gpt-5.6-sol")
        ident2 = resolve_model_identity("claude-fable-5-thinking-high")
        self.assertEqual(ident2.canonical, "claude-fable-5")

    def test_provider_mapping(self) -> None:
        ident = resolve_model_identity("openai")
        self.assertIsNone(ident.canonical)
        self.assertEqual(ident.provider, "openai")
        self.assertEqual(ident.raw, "openai")

        with_hint = resolve_model_identity(
            "gpt-5.5", provider_hint="openai"
        )
        self.assertEqual(with_hint.canonical, "gpt-5.5")
        self.assertEqual(with_hint.provider, "openai")

    def test_agent_profile_separation(self) -> None:
        review = resolve_model_identity("codex-auto-review")
        self.assertIsNone(review.canonical)
        self.assertEqual(review.agent_profile, "codex-auto-review")

        build = resolve_model_identity("grok-4.5-build")
        self.assertEqual(build.canonical, "grok-4.5")
        self.assertEqual(build.agent_profile, "grok-4.5-build")
        self.assertEqual(build.provider, "xai")

        with_role = resolve_model_identity(
            None, provider_hint="openai", agent_profile_hint="explorer"
        )
        self.assertIsNone(with_role.canonical)
        self.assertEqual(with_role.provider, "openai")
        self.assertEqual(with_role.agent_profile, "explorer")

    def test_unknown_handling(self) -> None:
        self.assertEqual(
            resolve_model_identity(None),
            resolve_model_identity(""),
        )
        empty = resolve_model_identity(None)
        self.assertIsNone(empty.canonical)
        self.assertIsNone(empty.raw)

        synthetic = resolve_model_identity("<synthetic>")
        self.assertIsNone(synthetic.canonical)
        self.assertEqual(synthetic.raw, "<synthetic>")
        self.assertEqual(display_model(synthetic.canonical), UNKNOWN_MODEL_LABEL)
        self.assertEqual(display_model(None), UNKNOWN_MODEL_LABEL)

    def test_known_non_models_never_canonical(self) -> None:
        for raw in (
            "codex-auto-review",
            "openai",
            "<synthetic>",
            "anthropic",
            "default",
            "auto",
        ):
            ident = resolve_model_identity(raw)
            self.assertIsNone(
                ident.canonical,
                msg=f"{raw!r} must not land in model_canonical",
            )
            self.assertTrue(is_known_non_model(raw))

    def test_registry_covers_canonical_ids(self) -> None:
        for mid in (
            "gpt-5.5",
            "grok-4.5",
            "claude-opus-5",
            "kimi-k3-max",
            "composer-2.5",
        ):
            self.assertIn(mid, MODELS_BY_ID)
        self.assertIn("codex-auto-review", AGENT_PROFILES)
        self.assertIn("openai", PROVIDERS)


class ModelIdentityGuardTests(unittest.TestCase):
    """Fails if a known non-model value ever resolves as canonical."""

    NON_MODELS = (
        "codex-auto-review",
        "openai",
        "anthropic",
        "<synthetic>",
        "synthetic",
        "default",
        "auto",
        "none",
        "null",
        "unknown",
    )

    def test_guard_non_models_out_of_canonical(self) -> None:
        for raw in self.NON_MODELS:
            with self.subTest(raw=raw):
                self.assertIsNone(resolve_model_identity(raw).canonical)


if __name__ == "__main__":
    unittest.main()
