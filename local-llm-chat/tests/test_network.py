"""Network contract tests; all HTTP calls use an in-process mock transport."""

import asyncio
import json
import socket
import ssl
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from chat_app import network


REAL_CLIENT = httpx.AsyncClient
SETTINGS = {"base_url": "http://localhost:1234/v1", "model": "local-model", "api_key": "private-api-key",
            "proxy_mode": "direct", "temperature": 0.7, "max_tokens": 2048}


def frame(delta=None, finish=None):
    return "data: " + json.dumps({"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}) + "\n\n"


def stream_response(*frames):
    return httpx.Response(200, text="".join(frames), headers={"content-type": "text/event-stream"})


class NetworkTests(unittest.IsolatedAsyncioTestCase):
    def client_factory(self, handler, captured=None):
        def factory(**kwargs):
            if captured is not None:
                captured.append(kwargs.copy())
            kwargs["transport"] = httpx.MockTransport(handler)
            # Proxy routing would bypass the mock transport. Its configuration
            # is checked independently, and never performs a live connection.
            kwargs.pop("proxy", None)
            kwargs.pop("mounts", None)
            kwargs["trust_env"] = False
            return REAL_CLIENT(**kwargs)
        return factory

    async def collect(self, settings=None, web=False):
        return [event async for event in network.stream_chat(settings or SETTINGS, [{"role": "user", "content": "質問"}], web)]

    async def test_models_normalization_and_credentials_only_for_llm(self):
        captured = []
        def handler(request):
            self.assertEqual(str(request.url), "http://localhost:1234/custom/models")
            self.assertEqual(request.headers["authorization"], "Bearer private-api-key")
            return httpx.Response(200, json={"data": [{"id": "a"}, {"id": "a"}, {"id": "b"}]})
        settings = {**SETTINGS, "base_url": "http://localhost:1234/custom/", "proxy_mode": "manual", "proxy_url": "http://user@proxy.example:8080", "proxy_password": "secret-proxy"}
        with patch.object(network.httpx, "AsyncClient", side_effect=self.client_factory(handler, captured)):
            self.assertEqual(await network.list_models(settings), ["a", "b"])
        self.assertFalse(captured[0]["trust_env"])
        self.assertNotIn("proxy", captured[0])
        self.assertIsInstance(captured[0]["verify"], ssl.SSLContext)
        self.assertTrue(captured[0]["verify"].check_hostname)

    async def test_proxy_modes_are_only_applied_to_web_client(self):
        for mode in ("direct", "environment", "manual"):
            captured = []
            settings = {**SETTINGS, "proxy_mode": mode, "proxy_url": "http://domain%5Cuser@proxy.example:8080", "proxy_password": "sensitive"}
            with patch.object(network.httpx, "AsyncClient", side_effect=self.client_factory(lambda _: httpx.Response(200), captured)):
                async with network._web_client(settings):
                    pass
            config = captured[0]
            self.assertEqual(config["trust_env"], mode == "environment")
            self.assertNotIn("Authorization", config["headers"])
            self.assertNotIn("Cookie", config["headers"])
            if mode == "manual":
                self.assertEqual(str(config["proxy"].url), "http://proxy.example:8080")
                self.assertEqual(config["proxy"].auth, ("domain\\user", "sensitive"))
            else:
                self.assertIsNone(config["proxy"])

    async def test_https_proxy_uses_the_configured_ca_context(self):
        captured = []
        context = ssl.create_default_context()
        settings = {**SETTINGS, "proxy_mode": "manual", "proxy_url": "https://user@proxy.example:8443", "proxy_password": "sensitive", "ca_bundle": "company-ca.pem"}
        with patch.object(network, "_verify", return_value=context) as verify, patch.object(network.httpx, "AsyncClient", side_effect=self.client_factory(lambda _: httpx.Response(200), captured)):
            async with network._web_client(settings):
                pass
        verify.assert_called_once_with(settings)
        self.assertIs(captured[0]["verify"], context)
        self.assertIs(captured[0]["proxy"].ssl_context, context)
        self.assertTrue(captured[0]["proxy"].ssl_context.check_hostname)

    async def test_environment_https_proxy_uses_ca_and_preserves_no_proxy(self):
        captured = []
        context = ssl.create_default_context()
        settings = {**SETTINGS, "proxy_mode": "environment", "ca_bundle": "company-ca.pem"}
        transport = httpx.MockTransport(lambda _: httpx.Response(200))
        env = {"https": "https://user:secret@proxy.example:8443", "no": "example.com"}
        with patch.object(network, "_verify", return_value=context), patch.object(network, "getproxies", return_value=env), patch.object(network.httpx, "AsyncHTTPTransport", return_value=transport) as build_transport, patch.object(network.httpx, "AsyncClient", side_effect=self.client_factory(lambda _: httpx.Response(200), captured)):
            async with network._web_client(settings):
                pass
        args = build_transport.call_args.kwargs
        self.assertIs(args["proxy"].ssl_context, context)
        self.assertIs(args["verify"], context)
        self.assertFalse(args["trust_env"])
        self.assertEqual(captured[0]["mounts"], {"https://": transport})
        self.assertTrue(captured[0]["trust_env"])  # httpx retains native NO_PROXY rules.
        with patch.object(network, "_verify", return_value=context), patch.object(network, "getproxies", return_value={**env, "no": "*"}), patch.object(network.httpx, "AsyncClient", side_effect=self.client_factory(lambda _: httpx.Response(200), captured)):
            async with network._web_client(settings):
                pass
        self.assertIsNone(captured[-1]["mounts"])

    async def test_plain_stream_has_no_tools_and_handles_unicode(self):
        def handler(request):
            payload = json.loads(request.content)
            self.assertNotIn("tools", payload)
            self.assertNotIn("tool_choice", payload)
            return stream_response(": heartbeat\n\n", frame({"role": "assistant"}), frame({"content": "こんにちは"}), frame(finish="stop"), "data: [DONE]\n\n")
        with patch.object(network.httpx, "AsyncClient", side_effect=self.client_factory(handler)):
            events = await self.collect()
        self.assertEqual(events, [{"event": "delta", "data": {"content": "こんにちは"}}])

    async def test_stream_tool_arguments_are_accumulated_then_returned(self):
        requests = []
        def handler(request):
            payload = json.loads(request.content)
            requests.append(payload)
            if len(requests) == 1:
                return stream_response(
                    frame({"content": "確認します。"}),
                    frame({"tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "web_", "arguments": '{"url":"https://'}}]}),
                    frame({"tool_calls": [{"index": 0, "function": {"name": "fetch", "arguments": 'example.com/"}'}}]}),
                    frame(finish="tool_calls"), "data: [DONE]\n\n")
            self.assertEqual(payload["messages"][-1]["role"], "tool")
            result = json.loads(payload["messages"][-1]["content"])
            self.assertTrue(result["untrusted_external_content"])
            self.assertEqual(result["text"], "参考本文")
            self.assertEqual(payload["messages"][-2]["tool_calls"][0]["function"]["name"], "web_fetch")
            return stream_response(frame({"content": "回答です。"}), frame(finish="stop"), "data: [DONE]\n\n")
        fetch = AsyncMock(return_value={"url": "https://example.com/", "title": "参考", "text": "参考本文"})
        with patch.object(network.httpx, "AsyncClient", side_effect=self.client_factory(handler)), patch.object(network, "fetch_web", fetch):
            events = await self.collect(web=True)
        fetch.assert_awaited_once_with(SETTINGS, "https://example.com/")
        self.assertEqual(len(requests), 2)
        self.assertIn({"event": "source", "data": {"url": "https://example.com/", "title": "参考"}}, events)
        self.assertEqual([e["data"]["content"] for e in events if e["event"] == "delta"], ["確認します。", "回答です。"])

    async def test_unknown_tools_never_execute(self):
        requests = []
        def handler(request):
            requests.append(json.loads(request.content))
            if len(requests) == 1:
                return stream_response(frame({"tool_calls": [{"index": 0, "id": "call_bad", "function": {"name": "shell", "arguments": "{}"}}]}), frame(finish="tool_calls"), "data: [DONE]\n\n")
            self.assertIn("error", json.loads(requests[-1]["messages"][-1]["content"]))
            return stream_response(frame({"content": "利用できません。"}), frame(finish="stop"), "data: [DONE]\n\n")
        fetch = AsyncMock()
        with patch.object(network.httpx, "AsyncClient", side_effect=self.client_factory(handler)), patch.object(network, "fetch_web", fetch):
            events = await self.collect(web=True)
        fetch.assert_not_awaited()
        self.assertTrue(any(e["event"] == "warning" for e in events))

    async def test_fetch_limit_and_final_request_omits_tools(self):
        request_count = 0
        def handler(request):
            nonlocal request_count
            request_count += 1
            payload = json.loads(request.content)
            if request_count == 1:
                calls = [{"index": i, "id": f"call_{i}", "function": {"name": "web_fetch", "arguments": json.dumps({"url": f"https://example.com/{i}"})}} for i in range(4)]
                return stream_response(frame({"tool_calls": calls}), frame(finish="tool_calls"), "data: [DONE]\n\n")
            self.assertNotIn("tools", payload)
            return stream_response(frame({"content": "完了"}), frame(finish="stop"), "data: [DONE]\n\n")
        fetch = AsyncMock(return_value={"url": "https://example.com/", "title": "情報", "text": "本文"})
        with patch.object(network.httpx, "AsyncClient", side_effect=self.client_factory(handler)), patch.object(network, "fetch_web", fetch):
            await self.collect(web=True)
        self.assertEqual(fetch.await_count, 4)
        self.assertEqual(request_count, 2)

    async def test_three_tool_rounds_then_final_answer(self):
        request_count = 0
        def handler(request):
            nonlocal request_count
            request_count += 1
            payload = json.loads(request.content)
            if request_count <= 3:
                call = {"index": 0, "id": f"call_{request_count}", "function": {"name": "web_fetch", "arguments": '{"url":"https://example.com/"}'}}
                return stream_response(frame({"tool_calls": [call]}), frame(finish="tool_calls"), "data: [DONE]\n\n")
            self.assertNotIn("tools", payload)
            return stream_response(frame({"content": "完了"}), frame(finish="stop"), "data: [DONE]\n\n")
        fetch = AsyncMock(return_value={"url": "https://example.com/", "title": "情報", "text": "本文"})
        with patch.object(network.httpx, "AsyncClient", side_effect=self.client_factory(handler)), patch.object(network, "fetch_web", fetch):
            await self.collect(web=True)
        self.assertEqual(fetch.await_count, 3)
        self.assertEqual(request_count, 4)

    async def test_web_results_share_remaining_context_and_ignore_image_base64(self):
        requests = []
        settings = {**SETTINGS, "context_chars": 8000}
        messages = [{"role": "user", "content": [{"type": "text", "text": "質問" * 250}, {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "x" * 100_000}}]}]
        def handler(request):
            payload = json.loads(request.content)
            requests.append(payload)
            if len(requests) == 1:
                self.assertIn("tools", payload)
                calls = [{"index": i, "id": f"call_{i}", "function": {"name": "web_fetch", "arguments": json.dumps({"url": f"https://example.com/{i}"})}} for i in range(4)]
                return stream_response(frame({"tool_calls": calls}), frame(finish="tool_calls"), "data: [DONE]\n\n")
            self.assertLessEqual(network._context_cost(payload["messages"]), settings["context_chars"])
            self.assertNotIn("tools", payload)
            results = [json.loads(message["content"]) for message in payload["messages"] if message["role"] == "tool"]
            self.assertEqual(len(results), 4)
            total_text = sum(len(result["text"]) for result in results)
            self.assertGreater(total_text, 500)
            self.assertLess(total_text, 6000)
            for result in results:
                self.assertEqual(result["url"], "https://example.com/")
                self.assertEqual(result["title"], "取得元タイトル")
                self.assertIn("参照文字数", result["warning"])
            return stream_response(frame({"content": "完了"}), frame(finish="stop"), "data: [DONE]\n\n")
        fetch = AsyncMock(return_value={"url": "https://example.com/", "title": "取得元タイトル", "text": '\\"\n' * 10_000})
        with patch.object(network.httpx, "AsyncClient", side_effect=self.client_factory(handler)), patch.object(network, "fetch_web", fetch):
            events = [event async for event in network.stream_chat(settings, messages, True)]
        self.assertEqual(fetch.await_count, 4)
        self.assertEqual(sum(event["event"] == "source" for event in events), 4)
        self.assertEqual(sum(event["event"] == "warning" for event in events), 4)

    async def test_no_reference_room_disables_web_before_fetch(self):
        settings = {**SETTINGS, "context_chars": 2000}
        def handler(request):
            payload = json.loads(request.content)
            self.assertNotIn("tools", payload)
            return stream_response(frame({"content": "現在の情報で回答"}), frame(finish="stop"), "data: [DONE]\n\n")
        messages = [{"role": "user", "content": "x" * 1000}]
        fetch = AsyncMock()
        with patch.object(network.httpx, "AsyncClient", side_effect=self.client_factory(handler)), patch.object(network, "fetch_web", fetch):
            events = [event async for event in network.stream_chat(settings, messages, True)]
        self.assertTrue(any(event["event"] == "warning" for event in events))
        fetch.assert_not_awaited()

    async def test_bad_empty_and_unfinished_streams_are_rejected(self):
        cases = ["data: {invalid}\n\n", "", "data: [DONE]\n\n", frame({"content": "未完了"}),
                 frame(finish="stop") + "data: [DONE]\n\n", "not sse\n\n", "data: []\n\n",
                 "data: {\"unexpected\":true}\n\n"]
        for content in cases:
            with self.subTest(content=content):
                with patch.object(network.httpx, "AsyncClient", side_effect=self.client_factory(lambda _: stream_response(content))):
                    with self.assertRaises(network.NetworkError):
                        await self.collect()

    async def test_usage_chunk_and_no_done_after_valid_finish_are_supported(self):
        usage = 'data: {"choices": [], "usage": {"total_tokens": 4}}\n\n'
        with patch.object(network.httpx, "AsyncClient", side_effect=self.client_factory(lambda _: stream_response(frame({"content": "本文"}), frame(finish="stop"), usage))):
            self.assertEqual((await self.collect())[0]["data"]["content"], "本文")

    async def test_http_and_transport_errors_do_not_expose_credentials(self):
        responses = [httpx.Response(401, text="private-api-key sensitive proxy.example"), httpx.Response(400, json={"error": {"message": "private-api-key"}})]
        for response in responses:
            with patch.object(network.httpx, "AsyncClient", side_effect=self.client_factory(lambda _, response=response: response)):
                with self.assertRaises(network.NetworkError) as caught:
                    await self.collect()
            self.assertNotIn("private-api-key", str(caught.exception))
            self.assertNotIn("sensitive", str(caught.exception))
        def failure(request):
            raise httpx.ConnectError("http://user:sensitive@proxy.example private-api-key", request=request)
        with patch.object(network.httpx, "AsyncClient", side_effect=self.client_factory(failure)):
            with self.assertRaises(network.NetworkError) as caught:
                await self.collect()
        self.assertNotIn("sensitive", str(caught.exception))
        self.assertNotIn("private-api-key", str(caught.exception))

    async def test_public_url_rejects_local_and_private_addresses(self):
        for url in ("http://localhost/x", "http://127.0.0.1", "http://10.0.0.1", "http://169.254.169.254", "http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://192.168.0.1", "file:///secret", "https://user:secret@example.com", "http://machine.internal"):
            with self.subTest(url=url):
                with self.assertRaises(network.NetworkError):
                    await network._public_url(url)
        loop = asyncio.get_running_loop()
        private_dns = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.10", 80))]
        with patch.object(loop, "getaddrinfo", AsyncMock(return_value=private_dns)):
            with self.assertRaises(network.NetworkError):
                await network._public_url("https://public-looking.example/")

    async def test_redirect_to_private_network_is_blocked_before_request(self):
        requested = []
        def handler(request):
            requested.append(str(request.url))
            return httpx.Response(302, headers={"location": "http://127.0.0.1/secrets"})
        with patch.object(network.httpx, "AsyncClient", side_effect=self.client_factory(handler)):
            with self.assertRaises(network.NetworkError):
                await network.fetch_web(SETTINGS, "https://93.184.216.34/")
        self.assertEqual(requested, ["https://93.184.216.34/"])

    async def test_redirect_does_not_forward_cookie_or_api_key(self):
        requested = []
        def handler(request):
            requested.append(request)
            self.assertNotIn("authorization", request.headers)
            self.assertNotIn("cookie", request.headers)
            self.assertNotIn("proxy-authorization", request.headers)
            if len(requested) == 1:
                return httpx.Response(302, headers={"location": "/article", "set-cookie": "session=secret; Path=/"})
            return httpx.Response(200, text="<html><head><title>Example &amp; title</title><style>hidden</style></head><body><h1>本文</h1><script>fetch('secret')</script><p>続き</p></body></html>", headers={"content-type": "text/html; charset=utf-8"})
        with patch.object(network.httpx, "AsyncClient", side_effect=self.client_factory(handler)):
            result = await network.fetch_web(SETTINGS, "https://93.184.216.34/")
        self.assertEqual(result["url"], "https://93.184.216.34/article")
        self.assertEqual(result["title"], "Example & title")
        self.assertIn("本文", result["text"])
        self.assertNotIn("fetch", result["text"])
        self.assertNotIn("hidden", result["text"])

    async def test_fetch_size_limit_and_text_truncation(self):
        def too_big(request):
            return httpx.Response(200, text="large", headers={"content-type": "text/plain", "content-length": str(network.MAX_WEB_BYTES + 1)})
        with patch.object(network.httpx, "AsyncClient", side_effect=self.client_factory(too_big)):
            with self.assertRaises(network.NetworkError):
                await network.fetch_web(SETTINGS, "https://93.184.216.34/")
        def long_page(request):
            return httpx.Response(200, text="x" * 40_100, headers={"content-type": "text/plain"})
        with patch.object(network.httpx, "AsyncClient", side_effect=self.client_factory(long_page)):
            result = await network.fetch_web(SETTINGS, "https://93.184.216.34/")
        self.assertEqual(len(result["text"]), 40_000)
        self.assertIn("warning", result)


if __name__ == "__main__":
    unittest.main()
