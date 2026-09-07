from __future__ import annotations

import io
import json
import sys
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentlog import cli  # noqa: E402
from agentlog.analysis.extractors.llm_client import (  # noqa: E402
    XAIChatClient,
    _NoRedirectHandler,
)
from agentlog.safety.egress import (  # noqa: E402
    ACKNOWLEDGEMENT,
    EgressBlocked,
    assert_egress_allowed,
    disable_remote_extraction,
    remote_extraction,
)


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()


class _RecordingOpener:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def open(self, request, *, timeout):
        self.urls.append(request.full_url)
        content = json.dumps({"result": "ok"})
        payload = {"choices": [{"message": {"content": content}}]}
        return _FakeResponse(json.dumps(payload).encode("utf-8"))


class ExactEndpointGateTests(unittest.TestCase):
    def tearDown(self) -> None:
        disable_remote_extraction()

    def test_same_host_different_destination_is_rejected(self) -> None:
        authorized = "https://extract.example/v1/chat/completions"
        alternatives = (
            "http://extract.example/v1/chat/completions",
            "https://extract.example:8443/v1/chat/completions",
            "https://extract.example/v2/chat/completions",
            "https://extract.example/v1/chat/completions?mode=other",
        )
        with remote_extraction(
            endpoint=authorized,
            acknowledgement=ACKNOWLEDGEMENT,
        ):
            assert_egress_allowed(authorized)
            for actual in alternatives:
                with self.subTest(actual=actual):
                    with self.assertRaises(EgressBlocked):
                        assert_egress_allowed(actual)

    def test_default_port_spelling_is_equivalent(self) -> None:
        with remote_extraction(
            endpoint="https://extract.example/v1/chat/completions",
            acknowledgement=ACKNOWLEDGEMENT,
        ):
            assert_egress_allowed(
                "https://extract.example:443/v1/chat/completions"
            )

    def test_endpoint_rejects_ambiguous_or_secret_bearing_urls(self) -> None:
        invalid = (
            "extract.example/v1/chat/completions",
            "file:///tmp/chat/completions",
            "https://name:credential@extract.example/v1/chat/completions",
            "https://extract.example/v1/chat/completions#fragment",
            "https://extract.example:invalid/v1/chat/completions",
        )
        for endpoint in invalid:
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(EgressBlocked):
                    with remote_extraction(
                        endpoint=endpoint,
                        acknowledgement=ACKNOWLEDGEMENT,
                    ):
                        pass


class ClientEndpointTests(unittest.TestCase):
    def tearDown(self) -> None:
        disable_remote_extraction()

    def test_custom_base_url_is_the_authorized_and_requested_destination(self) -> None:
        opener = _RecordingOpener()
        client = XAIChatClient(
            api_key="configured-test-key",
            base_url="https://extract.example:8443/v2",
            opener=opener,
        )
        with remote_extraction(
            endpoint=client.endpoint,
            acknowledgement=ACKNOWLEDGEMENT,
        ):
            result = client.complete_json(system="system", user="data", model="model")
        self.assertEqual(result, {"result": "ok"})
        self.assertEqual(
            opener.urls,
            ["https://extract.example:8443/v2/chat/completions"],
        )

    def test_cli_gate_returns_the_client_bound_to_the_disclosed_endpoint(self) -> None:
        with mock.patch.object(cli.console, "print"):
            client = cli._enable_remote_egress_or_exit(
                allow=True,
                acknowledgement=ACKNOWLEDGEMENT,
                endpoint="https://extract.example/v3",
            )
        self.assertEqual(
            client.endpoint,
            "https://extract.example/v3/chat/completions",
        )
        assert_egress_allowed(client.endpoint)

    def test_redirect_handler_refuses_a_second_destination(self) -> None:
        handler = _NoRedirectHandler()
        request = urllib.request.Request("https://extract.example/v1/chat/completions")
        redirected = handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://other.example/collect",
        )
        self.assertIsNone(redirected)


if __name__ == "__main__":
    unittest.main()
