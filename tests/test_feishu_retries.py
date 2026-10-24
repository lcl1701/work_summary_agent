import io
import json
from email.message import Message
from threading import Barrier, Lock, Thread
import unittest
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from app.feishu import FeishuClient, FeishuError
from tests.test_feishu import FakeOpener, page, token


def http_error(status, code=None, reset=None):
    headers = Message()
    if reset is not None:
        headers["x-ogw-ratelimit-reset"] = str(reset)
    body = json.dumps({"code": code, "msg": "unsafe-payload-secret"}).encode("utf-8")
    return HTTPError("https://open.feishu.cn", status, "unsafe-reason", headers, io.BytesIO(body))


class FeishuRetryTests(unittest.TestCase):
    def client(self, *responses):
        self.fake, self.waits = FakeOpener(*responses), []
        return FeishuClient("test-id", "test-secret", opener=self.fake, sleep=self.waits.append)

    def test_network_retries_are_bounded_and_keep_safe_error(self):
        client = self.client(URLError("unsafe-secret"), URLError("unsafe-secret"), URLError("unsafe-secret"))
        with self.assertRaisesRegex(FeishuError, "无法连接") as raised:
            client.list_chats()
        self.assertEqual(len(self.fake.requests), 3)
        self.assertEqual(self.waits, [0.2, 0.4])
        self.assertNotIn("unsafe-secret", str(raised.exception))

    def test_429_uses_official_wait_header_before_retry(self):
        client = self.client(token(), http_error(429, reset=2), page([]))
        self.assertEqual(client.list_chats(), [])
        self.assertEqual(self.waits, [2])

    def test_long_header_is_not_retried_earlier_than_recommended(self):
        client = self.client(token(), http_error(429, reset=60))
        with self.assertRaisesRegex(FeishuError, "频率"):
            client.list_chats()
        self.assertEqual(len(self.fake.requests), 2)
        self.assertEqual(self.waits, [])

    def test_5xx_and_rate_codes_retry_but_permissions_and_quota_do_not(self):
        client = self.client(token(), http_error(503, code=123456), {"code": 99991400}, page([]))
        self.assertEqual(client.list_chats(), [])
        self.assertEqual(self.waits, [0.2, 0.4])
        for code in (99991672, 99991403, 20002):
            client = self.client(token(), {"code": code})
            with self.subTest(code=code), self.assertRaises(FeishuError):
                client.list_chats()
            self.assertEqual(len(self.fake.requests), 2)
            self.assertEqual(self.waits, [])

    def test_member_open_ids_paginate_and_permission_error_is_specific(self):
        client = self.client(token(), page([{"member_id": "ou_one", "name": "同名成员"}], True, "next"),
                             page([{"member_id": "ou_two", "name": "同名成员"}]))
        self.assertEqual(client.list_members("oc_group"), [{"sender_id": "ou_one", "name": "同名成员"},
                                                          {"sender_id": "ou_two", "name": "同名成员"}])
        query = parse_qs(urlsplit(self.fake.requests[1].full_url).query)
        self.assertEqual(query["member_id_type"], ["open_id"])
        self.assertEqual(query["page_size"], ["100"])
        client = self.client(token(), {"code": 99991672})
        with self.assertRaisesRegex(FeishuError, "im:chat.members:read") as raised:
            client.list_members("oc_group")
        self.assertNotIn("im:message.group_msg", str(raised.exception))
        self.assertEqual(self.waits, [])

    def test_progress_is_per_call_and_includes_page_retry_events(self):
        client = self.client(token(), http_error(429, reset=1), page([], True, "next"), page([]), page([]))
        events = []
        client.list_messages("oc_group", 100, 200, progress=events.append)
        self.assertEqual(events, [{"phase": "fetch", "page": 1},
            {"phase": "retry", "attempt": 2, "wait": 1, "page": 1}, {"phase": "fetch", "page": 2}])
        client.list_chats()
        self.assertEqual(len(events), 3)

    def test_concurrent_calls_share_one_locked_token_refresh(self):
        lock, barrier = Lock(), Barrier(5)
        calls = {"token": 0, "get": 0}
        errors = []
        def opener(request, timeout):
            with lock:
                key = "token" if request.method == "POST" else "get"
                calls[key] += 1
            return io.BytesIO(json.dumps(token() if request.method == "POST" else page([])).encode())
        client = FeishuClient("id", "secret", opener=opener, sleep=lambda _: None)
        def run():
            try:
                barrier.wait(timeout=5)
                client.list_chats()
            except Exception as exc:
                errors.append(exc)
        threads = [Thread(target=run) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(errors, [])
        self.assertEqual(calls, {"token": 1, "get": 5})


if __name__ == "__main__":
    unittest.main()
