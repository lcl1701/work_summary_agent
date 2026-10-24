import io
import json
import unittest
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

from app.feishu import FeishuClient, FeishuError, _NoRedirect


def page(items, more=False, token=None):
    data = {"items": items, "has_more": more}
    if token is not None:
        data["page_token"] = token
    return {"code": 0, "data": data}


def message(message_id="om_1", text="完成接口开发", timestamp="1792368000123", msg_type="text", **extra):
    value = {"message_id": message_id, "msg_type": msg_type, "create_time": timestamp,
             "sender": {"id": "ou_1"}, "body": {"content": json.dumps({"text": text})}}
    value.update(extra)
    return value


class FakeOpener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("Unexpected network request")
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return io.BytesIO(value if isinstance(value, bytes) else json.dumps(value).encode("utf-8"))


def token(value="test-token", expires=7200):
    return {"code": 0, "tenant_access_token": value, "expire": expires}


class FeishuClientTests(unittest.TestCase):
    def test_chats_paginate_and_reuse_token(self):
        fake = FakeOpener(token(), page([{"chat_id": "oc_1", "name": "研发", "description": "demo"}], True, "next"),
                          page([{"chat_id": "oc_2", "name": "产品"}]), page([]))
        client = FeishuClient("cli_id", "secret", opener=fake)
        self.assertEqual([chat["chat_id"] for chat in client.list_chats()], ["oc_1", "oc_2"])
        self.assertEqual(client.list_chats(), [])
        self.assertEqual(len(fake.requests), 4)
        self.assertEqual(fake.requests[0].method, "POST")
        self.assertEqual(json.loads(fake.requests[0].data), {"app_id": "cli_id", "app_secret": "secret"})
        self.assertEqual(parse_qs(urlparse(fake.requests[2].full_url).query)["page_token"], ["next"])
        self.assertEqual(fake.requests[1].get_header("Authorization"), "Bearer test-token")

    def test_messages_use_seconds_and_normalize_milliseconds_with_exclusive_end(self):
        fake = FakeOpener(token(), page([message("om_a"), message("om_deleted", deleted=True),
                                        message("om_system", msg_type="system"), message("om_empty", text="  "),
                                        message("om_image", msg_type="image")], True, "page2"),
                          page([message("om_b", timestamp="1792368001999"), message("om_end", timestamp="1792368002000")]))
        rows = FeishuClient("id", "secret", opener=fake).list_messages("oc_1", 1792368000, 1792368002)
        self.assertEqual([row["message_id"] for row in rows], ["om_a", "om_b"])
        self.assertEqual(rows[0]["created_at"], 1792368000)
        self.assertEqual(rows[0]["sender_name"], "ou_1")
        params = parse_qs(urlparse(fake.requests[1].full_url).query)
        self.assertEqual(params["start_time"], ["1792368000"])
        self.assertEqual(params["end_time"], ["1792368002"])
        self.assertEqual(params["page_size"], ["50"])
        self.assertEqual(params["sort_type"], ["ByCreateTimeAsc"])
        self.assertEqual(params["container_id_type"], ["chat"])

    def test_post_and_mentions_are_readable_without_extra_contact_requests(self):
        body = {"zh_cn": {"title": "项目进展", "content": [
            [{"tag": "text", "text": "已上线 "}, {"tag": "a", "text": "文档", "href": "https://example.com"}],
            [{"tag": "at", "user_name": "小李", "user_id": "ou_2"}, {"tag": "text", "text": " 请验收"}],
            [{"tag": "img", "image_key": "ignored"}],
        ]}}
        post = message("om_post", msg_type="post", body={"content": json.dumps(body)},
                       sender={"id": "ou_1", "sender_name": "小王"})
        mention = message("om_text", text="@_user_1 已完成", mentions=[{"key": "@_user_1", "name": "小李"}])
        fake = FakeOpener(token(), page([post, mention]))
        rows = FeishuClient("id", "secret", opener=fake).list_messages("oc_1", 1792368000, 1792368002)
        self.assertEqual(rows[0]["text"], "项目进展\n已上线 文档\n@小李 请验收")
        self.assertEqual(rows[0]["sender_name"], "小王")
        self.assertEqual(rows[1]["text"], "@小李 已完成")
        self.assertEqual(len(fake.requests), 2)

    def test_token_refreshes_before_expiration(self):
        now = [1000.0]
        fake = FakeOpener(token("first", 100), page([]), page([]), token("second", 100), page([]))
        client = FeishuClient("id", "secret", opener=fake, clock=lambda: now[0])
        client.list_chats()
        now[0] = 1089
        client.list_chats()
        now[0] = 1090
        client.list_chats()
        self.assertEqual(fake.requests[-1].get_header("Authorization"), "Bearer second")
        self.assertEqual(sum(request.method == "POST" for request in fake.requests), 2)

    def test_invalid_cached_token_refreshes_once(self):
        fake = FakeOpener(token("old"), {"code": 99991663}, token("new"), page([]))
        self.assertEqual(FeishuClient("id", "secret", opener=fake).list_chats(), [])
        self.assertEqual(fake.requests[-1].get_header("Authorization"), "Bearer new")

    def test_permission_and_token_errors_never_echo_secrets(self):
        for code, expected in [(99991672, "权限"), (20002, "凭证"), (230002, "目标群")]:
            with self.subTest(code=code):
                fake = FakeOpener(token(), {"code": code, "msg": "leaked-super-secret test-token"})
                with self.assertRaises(FeishuError) as raised:
                    FeishuClient("id", "leaked-super-secret", opener=fake).list_chats()
                text = str(raised.exception)
                self.assertIn(expected, text)
                self.assertNotIn("leaked-super-secret", text)
                self.assertNotIn("test-token", text)
                self.assertEqual(raised.exception.code, code)

    def test_http_error_body_preserves_safe_api_code(self):
        error = HTTPError("https://open.feishu.cn", 400, "unsafe-secret", {}, io.BytesIO(b'{"code":230006,"msg":"unsafe-secret"}'))
        fake = FakeOpener(token(), error)
        with self.assertRaisesRegex(FeishuError, "机器人能力") as raised:
            FeishuClient("id", "secret", opener=fake).list_chats()
        self.assertNotIn("unsafe-secret", str(raised.exception))

    def test_malformed_or_repeated_pagination_fails(self):
        for response in [page([], True), page([], True, "next"), {"code": 0, "data": {"has_more": "false"}}]:
            with self.subTest(response=response):
                fake = FakeOpener(token(), page([], True, "next"), response)
                with self.assertRaisesRegex(FeishuError, "分页"):
                    FeishuClient("id", "secret", opener=fake).list_chats()

    def test_invalid_token_payload_and_json_fail(self):
        for payload in [{"code": 0, "expire": 7200}, {"code": 0, "tenant_access_token": "t", "expire": 0},
                        {"code": 0, "tenant_access_token": "bad\r\nsecret", "expire": 7200},
                        b"not-json", {"data": {}}, {"code": True}]:
            with self.subTest(payload=payload):
                with self.assertRaises(FeishuError):
                    FeishuClient("id", "secret", opener=FakeOpener(payload)).list_chats()

    def test_malformed_text_fails_instead_of_silent_omission(self):
        fake = FakeOpener(token(), page([message(body={"content": "not-json"})]))
        with self.assertRaisesRegex(FeishuError, "正文格式"):
            FeishuClient("id", "secret", opener=fake).list_messages("oc", 1792368000, 1792368002)

    def test_network_error_is_safe(self):
        with self.assertRaisesRegex(FeishuError, "无法连接") as raised:
            FeishuClient("id", "secret", opener=FakeOpener(
                URLError("unsafe-secret"), URLError("unsafe-secret"), URLError("unsafe-secret")
            ), sleep=lambda _: None).list_chats()
        self.assertNotIn("unsafe-secret", str(raised.exception))

    def test_duplicate_messages_are_deduplicated_across_pages(self):
        fake = FakeOpener(token(), page([message()], True, "next"), page([message()]))
        rows = FeishuClient("id", "secret", opener=fake).list_messages("oc", 1792368000, 1792368002)
        self.assertEqual(len(rows), 1)

    def test_rejects_invalid_message_timestamp(self):
        for timestamp in [True, 1792368000123.5, "bad-time"]:
            with self.subTest(timestamp=timestamp):
                fake = FakeOpener(token(), page([message(timestamp=timestamp)]))
                with self.assertRaisesRegex(FeishuError, "创建时间"):
                    FeishuClient("id", "secret", opener=fake).list_messages("oc", 1792368000, 1792368002)

    def test_rejects_untrusted_endpoint_and_redirects(self):
        for url in ["http://open.feishu.cn", "https://evil.example", "https://open.feishu.cn@evil.example", "https://open.feishu.cn/path"]:
            with self.subTest(url=url):
                with self.assertRaises(FeishuError):
                    FeishuClient("id", "secret", base_url=url)
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.example"))


if __name__ == "__main__":
    unittest.main()
