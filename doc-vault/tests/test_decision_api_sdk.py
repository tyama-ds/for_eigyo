"""判定 API の要求・応答の形が、OpenAI の公式 Python SDK と一致することの確認。

公式 SDK（openai 3.46 以降。CI では入れて実行する）に同じ質問を送らせ、送られる URL・ヘッダー・本文を
Doc Vault が組み立てるものと比べる。モックの応答は SDK の Decision 型で検証してから読み取る。
SDK が入っていなければ飛ばす（アプリ自体は SDK を使わない）。
"""
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from docvault import decision_api as da  # noqa: E402
from mock_llm import openai_decision_answer  # noqa: E402

HAS_SDK = importlib.util.find_spec("openai") is not None


@unittest.skipUnless(HAS_SDK, "openai SDK が入っていない")
class TestMatchesOfficialSDK(unittest.TestCase):
    def setUp(self):
        import openai
        try:
            import httpx2 as httpx
        except ImportError:                       # 古い SDK は httpx
            import httpx
        self.openai, self.httpx = openai, httpx
        if not hasattr(openai.OpenAI(api_key="k"), "decisions"):
            self.skipTest("この版の openai SDK には Decisions API が無い")
        self.sent = []

    def client(self, cls=None, **kw):
        def handler(request):
            body = json.loads(request.content.decode("utf-8"))
            self.sent.append({"url": str(request.url), "headers": request.headers, "body": body})
            return self.httpx.Response(200, json=openai_decision_answer(body))
        http = self.openai.DefaultHttpxClient(transport=self.httpx.MockTransport(handler))
        return (cls or self.openai.OpenAI)(http_client=http, max_retries=0, **kw)

    def eff(self, provider, **sub):
        return da.effective({"decision_api": {"provider": provider, provider: sub}})

    def validate_request(self, body):
        """SDK の要求の型（DecisionCreateParams と質問・選択肢の型）で、項目名・型・type の値を検査する。

        Iterable の項目（questions・choices）は pydantic が中身を検査しないので、1つずつ検査する。
        """
        import pydantic
        from openai.types import decision_create_params as p
        pydantic.TypeAdapter(p.DecisionCreateParams).validate_python(body)
        self.assertTrue(set(body) <= set(p.DecisionCreateParams.__annotations__), body.keys())
        kinds = {"predicate": p.QuestionQuestionParamPredicate, "choice": p.QuestionQuestionParamChoice}
        for q in body["questions"]:
            cls = kinds[q["type"]]
            pydantic.TypeAdapter(cls).validate_python(q)
            self.assertTrue(set(q) <= set(cls.__annotations__), q.keys())     # 綴りの誤りが無い
            for c in q.get("choices", []):
                pydantic.TypeAdapter(p.QuestionQuestionParamChoiceChoice).validate_python(c)
                self.assertTrue(set(c) <= set(p.QuestionQuestionParamChoiceChoice.__annotations__))
            if q["type"] == "choice":
                self.assertTrue(2 <= len(q["choices"]) <= 255)     # SDK の説明: 選択肢は 2〜255 件
        self.assertTrue(1 <= len(body["questions"]) <= 400)        # SDK の説明: 質問は 1〜400 件

    def check(self, sdk_client, e, query, descs):
        from openai.types import Decision
        for build in (da.build_binary_request, da.build_choice_request):
            ours = build(e["provider"], e["model"], query, descs)
            self.validate_request(ours)
            sdk_client.decisions.create(**ours)                    # SDK に同じ要求を送らせる
            got = self.sent[-1]
            self.assertEqual(got["url"], da.request_url(e))
            self.assertEqual(got["body"], json.loads(json.dumps(ours, ensure_ascii=False)))
            # モックの応答を SDK の Decision 型で厳密に検証し、それを Doc Vault の読み取りに通す
            resp = Decision.model_validate(openai_decision_answer(got["body"])).model_dump(mode="json")
            if build is da.build_binary_request:
                probs, refused = da.parse_binary(e["provider"], resp, len(descs))
                self.assertEqual(len(probs), len(descs))
                self.assertGreater(probs[1], probs[2])
            else:
                probs = da.parse_choice(e["provider"], resp, len(descs))
                self.assertAlmostEqual(sum(probs.values()), 1.0, places=5)
                self.assertEqual(max(probs, key=probs.get), 1)
        return got

    def test_openai(self):
        e = self.eff("openai", api_key="sk-test")
        got = self.check(self.client(api_key="sk-test"), e, "富士山の標高は？",
                         ["富士山の標高は 3,776 m", "東京駅の開業は 1914 年", "琵琶湖の面積"])
        self.assertEqual(got["headers"]["authorization"], da.request_headers(e)["Authorization"])

    def test_azure_with_api_version(self):
        e = self.eff("azure", endpoint="https://res.openai.azure.com", api_key="az-test",
                     model="luna-deploy", api_version="2026-10-01-preview")
        sdk = self.client(self.openai.AzureOpenAI, api_key="az-test",
                          azure_endpoint="https://res.openai.azure.com", api_version="2026-10-01-preview")
        got = self.check(sdk, e, "富士山の標高は？", ["富士山の標高は 3,776 m", "東京駅の開業は 1914 年"])
        self.assertEqual(got["headers"]["api-key"], da.request_headers(e)["api-key"])

    def test_azure_v1(self):
        e = self.eff("azure", endpoint="https://res.openai.azure.com", api_key="az-test", model="luna-deploy")
        sdk = self.client(api_key="az-test", base_url="https://res.openai.azure.com/openai/v1/")
        self.check(sdk, e, "富士山の標高は？", ["富士山の標高は 3,776 m", "東京駅の開業は 1914 年"])


if __name__ == "__main__":
    unittest.main()
