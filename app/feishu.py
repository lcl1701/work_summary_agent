"""Read-only Feishu bot connector using the Python standard library.

Only text and post (rich text) messages are imported. Deleted/system messages,
cards, files, media, forwarded bundles and thread replies are outside this demo.
Sender names use server-provided names and fall back to IDs without requiring
additional contact-directory permissions.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from http.client import HTTPException
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


class FeishuError(RuntimeError):
    """A safe, actionable error. Upstream response text is never exposed."""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # App credentials and bearer tokens must never follow a redirect.
        return None


_TOKEN_CODES = {4001, 20013, 99991663, 99991665, 99991668}
_PERMISSION_CODES = {230027, 99991672, 99991676, 99991679}
_AUTH_CODES = {10005, 10015, 20002, 20048, 20130, 99991543}
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_PAGES = 10000
_RATE_CODES = {99991400, 99991401, 1000004, 1000005}


class _TransientError(Exception):
    def __init__(self, error, wait=None):
        self.error, self.wait = error, wait


def _retry_wait(headers):
    for key in ("x-ogw-ratelimit-reset", "Retry-After"):
        try:
            value = float((headers or {}).get(key, ""))
            if math.isfinite(value) and value >= 0:
                return value
        except (ValueError, TypeError):
            pass
    return None


def _api_error(code: int) -> FeishuError:
    if code in _PERMISSION_CODES:
        detail = (
            "飞书应用缺少读取权限。请在开放平台申请 im:chat:read、"
            "im:message:readonly 和 im:message.group_msg，完成管理员审批并发布版本。"
        )
    elif code in {10002, 230002, 232011}:
        detail = "机器人不在目标群中，请先将此应用的机器人加入群聊。"
    elif code in {230006, 232025}:
        detail = "飞书应用未启用机器人能力，请添加机器人能力并发布版本。"
    elif code in _AUTH_CODES:
        detail = "飞书应用凭证无效，请在本地配置中检查 App ID 和 App Secret。"
    elif code in {20009, 20069, 232034, 99991662, 99991673}:
        detail = "飞书应用未在当前企业安装或启用，请检查发布和启用状态。"
    elif code in _TOKEN_CODES:
        detail = "飞书访问凭证无效或已过期，请检查应用状态后重试。"
    elif code in {230013, 230073}:
        detail = "目标会话或历史消息不在机器人可见范围内，请检查应用可用范围和群历史设置。"
    elif code == 231203:
        detail = "此群聊类型或保密设置不支持读取历史消息。"
    elif code in {99991403}:
        detail = "飞书应用本月 API 调用额度已用完，请联系企业管理员。"
    elif code in {99991400, 99991401, 1000004, 1000005}:
        detail = "飞书接口请求频率过高，请稍后重试。"
    else:
        detail = "飞书接口请求失败，请根据错误码在飞书开放平台检查配置。"
    return FeishuError(f"{detail}（错误码 {code}）", code)


class FeishuClient:
    """List the app bot's groups and retrieve messages in [start_ts, end_ts).

    ``base_url`` is deliberately restricted to the official Feishu host. Tests
    may inject an ``opener(Request, timeout=...)`` callable and monotonic clock.
    The compatible internal-app token endpoint is used for this minimal demo.
    """

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        base_url: str = "https://open.feishu.cn",
        timeout: float = 20,
        *,
        opener: Callable | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ):
        if not isinstance(app_id, str) or not app_id.strip():
            raise FeishuError("请先配置 FEISHU_APP_ID。")
        if not isinstance(app_secret, str) or not app_secret.strip():
            raise FeishuError("请先配置 FEISHU_APP_SECRET。")
        if not isinstance(base_url, str) or base_url.rstrip("/") != "https://open.feishu.cn":
            raise FeishuError("飞书连接地址仅支持官方 HTTPS 地址 https://open.feishu.cn。")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 120:
            raise FeishuError("飞书请求超时必须在 0 到 120 秒之间。")
        self._app_id = app_id.strip()
        self._app_secret = app_secret.strip()
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._opener = opener or build_opener(_NoRedirect()).open
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._token_lock = Lock()
        self._token = ""
        self._token_expires_at = 0.0

    def _request_json(self, request: Request, progress=None) -> dict:
        waited = 0.0
        for attempt in range(3):
            try:
                return self._request_json_once(request)
            except _TransientError as exc:
                delay = exc.wait if exc.wait is not None else 0.2 * (2 ** attempt)
                if attempt == 2 or waited + delay > 6:
                    raise exc.error from None
                if progress:
                    progress({"phase": "retry", "attempt": attempt + 2, "wait": delay})
                self._sleep(delay)
                waited += delay
        raise FeishuError("飞书暂时无法连接，请稍后重试。")

    def _request_json_once(self, request: Request) -> dict:
        response_headers = None
        try:
            with self._opener(request, timeout=self._timeout) as response:
                response_headers = getattr(response, "headers", None)
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            # Feishu returns useful API codes with HTTP 400/403 as well as 200.
            try:
                raw = exc.read(_MAX_RESPONSE_BYTES + 1)
                payload = json.loads(raw)
                code = payload.get("code") if isinstance(payload, dict) else None
            except (ValueError, OSError):
                code = None
            finally:
                exc.close()
            terminal = _PERMISSION_CODES | _AUTH_CODES | _TOKEN_CODES | {99991403, 232011, 232025, 231203}
            if type(code) is int and code != 0 and code not in _RATE_CODES and (
                code in terminal or (exc.code != 429 and not 500 <= exc.code < 600)
            ):
                raise _api_error(code) from None
            wait = _retry_wait(exc.headers)
            if type(code) is int and code in _RATE_CODES:
                raise _TransientError(_api_error(code), wait) from None
            if exc.code == 429:
                raise _TransientError(FeishuError("飞书接口请求频率过高，请稍后重试。"), wait) from None
            if exc.code in {401, 403}:
                raise FeishuError("飞书拒绝访问，请检查应用凭证、读取权限和机器人入群状态。") from None
            if 300 <= exc.code < 400:
                raise FeishuError("飞书接口返回重定向，已停止请求以保护应用凭证。") from None
            if 500 <= exc.code < 600:
                raise _TransientError(FeishuError(f"飞书服务暂时不可用（HTTP {exc.code}），请稍后重试。"), wait) from None
            raise FeishuError(f"飞书服务请求失败（HTTP {exc.code}），请稍后重试。") from None
        except (URLError, OSError, TimeoutError, HTTPException):
            raise _TransientError(FeishuError("无法连接飞书，请检查网络或代理设置后重试。")) from None
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise FeishuError("飞书响应过大，已停止读取，请缩小同步范围。")
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError):
            raise FeishuError("飞书返回了无法解析的响应，请稍后重试。") from None
        if not isinstance(payload, dict) or type(payload.get("code")) is not int:
            raise FeishuError("飞书响应格式异常：缺少有效的状态码。")
        if payload["code"] != 0:
            if payload["code"] in _RATE_CODES:
                raise _TransientError(_api_error(payload["code"]), _retry_wait(response_headers))
            raise _api_error(payload["code"])
        return payload

    def _get_token(self) -> str:
        with self._token_lock:
            return self._load_token()

    def _load_token(self) -> str:
        if self._token and self._clock() < self._token_expires_at:
            return self._token
        request = Request(
            self._base_url + "/open-apis/auth/v3/tenant_access_token/internal",
            data=json.dumps({"app_id": self._app_id, "app_secret": self._app_secret}).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        payload = self._request_json(request)
        token, expires = payload.get("tenant_access_token"), payload.get("expire")
        if not isinstance(token, str) or not token or any(char.isspace() for char in token) or type(expires) is not int or expires <= 0:
            raise FeishuError("飞书授权响应格式异常：缺少有效的访问凭证或有效期。")
        self._token = token
        self._token_expires_at = self._clock() + expires - min(60, expires * 0.1)
        return token

    def _get(self, path: str, params: dict, progress=None) -> dict:
        for attempt in range(2):
            token = self._get_token()
            request = Request(
                self._base_url + path + "?" + urlencode(params),
                headers={"Authorization": "Bearer " + token, "Content-Type": "application/json; charset=utf-8"},
                method="GET",
            )
            try:
                return self._request_json(request, progress)
            except FeishuError as exc:
                if attempt == 0 and exc.code in _TOKEN_CODES:
                    with self._token_lock:
                        if self._token == token:
                            self._token = ""
                            self._token_expires_at = 0
                    continue
                raise
        raise FeishuError("飞书授权失败，请检查应用凭证。")

    def _pages(self, path: str, params: dict, progress=None):
        params = dict(params)
        seen_tokens = set()
        for page in range(1, _MAX_PAGES + 1):
            if progress:
                progress({"phase": "fetch", "page": page})
            callback = (lambda event, number=page: progress(dict(event, page=number))) if progress else None
            payload = self._get(path, params, callback)
            data = payload.get("data")
            if not isinstance(data, dict) or type(data.get("has_more")) is not bool:
                raise FeishuError("飞书分页响应格式异常，已停止同步。")
            if data["has_more"] and "items" not in data:
                raise FeishuError("飞书分页响应缺少消息列表，已停止同步。")
            items = data.get("items", [])
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise FeishuError("飞书列表响应格式异常，已停止同步。")
            yield items
            if not data["has_more"]:
                return
            token = data.get("page_token")
            if not isinstance(token, str) or not token.strip() or token in seen_tokens:
                raise FeishuError("飞书分页标记缺失或重复，已停止同步以避免数据不完整。")
            seen_tokens.add(token)
            params["page_token"] = token
        raise FeishuError("飞书分页数量超出 Demo 限制，请缩小同步范围。")

    def list_chats(self) -> list[dict]:
        chats = {}
        for items in self._pages("/open-apis/im/v1/chats", {"page_size": 50, "sort_type": "ByCreateTimeAsc"}):
            for item in items:
                chat_id = item.get("chat_id")
                if not isinstance(chat_id, str) or not chat_id:
                    raise FeishuError("飞书群列表缺少群 ID，已停止同步。")
                chats[chat_id] = {
                    "chat_id": chat_id,
                    "name": str(item.get("name") or chat_id),
                    "description": str(item.get("description") or ""),
                }
        return list(chats.values())

    def list_members(self, chat_id: str) -> list[dict]:
        if not isinstance(chat_id, str) or not chat_id.strip():
            raise FeishuError("请指定要查询的飞书群 ID。")
        members = {}
        try:
            for items in self._pages("/open-apis/im/v1/chats/" + quote(chat_id, safe="") + "/members",
                                     {"member_id_type": "open_id", "page_size": 100}):
                for item in items:
                    sender_id, name = item.get("member_id"), item.get("name")
                    if not isinstance(sender_id, str) or not sender_id or not isinstance(name, str):
                        raise FeishuError("飞书群成员响应格式异常，未更新本地姓名。")
                    members[sender_id] = {"sender_id": sender_id, "name": name}
        except FeishuError as exc:
            if exc.code in _PERMISSION_CODES:
                raise FeishuError("读取群成员姓名需要 im:chat.members:read 权限；请审批并发布后重试。消息同步可继续使用本地备注。", exc.code) from None
            raise
        return list(members.values())

    def list_messages(self, chat_id: str, start_ts: int, end_ts: int, progress=None) -> list[dict]:
        if not isinstance(chat_id, str) or not chat_id.strip():
            raise FeishuError("请指定要同步的飞书群 ID。")
        if type(start_ts) is not int or type(end_ts) is not int or start_ts < 0 or end_ts <= start_ts:
            raise FeishuError("同步时间需使用秒级时间戳，且结束时间应晚于开始时间。")
        params = {"container_id_type": "chat", "container_id": chat_id, "start_time": str(start_ts),
                  "end_time": str(end_ts), "page_size": 50, "sort_type": "ByCreateTimeAsc"}
        messages = {}
        for items in self._pages("/open-apis/im/v1/messages", params, progress):
            for item in items:
                message = _normalize_message(item, chat_id)
                if message and start_ts <= message["created_at"] < end_ts:
                    messages[message["message_id"]] = message
        return sorted(messages.values(), key=lambda row: (row["created_at"], row["message_id"]))


def _post_text(content: dict) -> str:
    # API receive bodies normally contain title/content directly. Also accept
    # locale-wrapped send-body shapes for consistent imported fixtures.
    if "content" not in content:
        candidates = [content.get("zh_cn"), content.get("en_us"), *content.values()]
        content = next((value for value in candidates if isinstance(value, dict) and "content" in value), {})
    lines = []
    title = content.get("title")
    if isinstance(title, str) and title.strip():
        lines.append(title.strip())
    rows = content.get("content", [])
    if not isinstance(rows, list):
        return "\n".join(lines)
    for row in rows:
        if not isinstance(row, list):
            continue
        parts = []
        for node in row:
            if not isinstance(node, dict):
                continue
            tag = node.get("tag")
            if tag in {"text", "md", "a"}:
                text = node.get("text", "")
                if tag == "a" and not text:
                    text = node.get("href", "")
                if isinstance(text, str):
                    parts.append(text)
            elif tag == "at":
                name = node.get("user_name") or node.get("user_id") or "成员"
                parts.append("@" + str(name))
        text = "".join(parts).strip()
        if text:
            lines.append(text)
    return "\n".join(lines).strip()


def _normalize_message(item: dict, chat_id: str) -> dict | None:
    message_type = item.get("msg_type")
    if item.get("deleted") or message_type not in {"text", "post"}:
        return None
    body = item.get("body")
    raw = body.get("content") if isinstance(body, dict) else None
    if isinstance(raw, str):
        try:
            content = json.loads(raw)
        except (ValueError, UnicodeError):
            raise FeishuError("飞书消息正文格式异常，已停止同步以避免遗漏。") from None
    else:
        content = raw
    if not isinstance(content, dict):
        raise FeishuError("飞书消息正文格式异常，已停止同步以避免遗漏。")
    text = content.get("text", "") if message_type == "text" else _post_text(content)
    if not isinstance(text, str) or not text.strip():
        return None
    # Replace Feishu text-message mention placeholders with readable names.
    mentions = item.get("mentions", [])
    if isinstance(mentions, list):
        for mention in mentions:
            if isinstance(mention, dict) and isinstance(mention.get("key"), str) and mention["key"]:
                text = text.replace(mention["key"], "@" + str(mention.get("name") or mention.get("id") or "成员"))
    message_id = item.get("message_id")
    try:
        raw_timestamp = item["create_time"]
        if type(raw_timestamp) not in {int, str}:
            raise ValueError
        timestamp = int(raw_timestamp)
        # Feishu's message create_time is milliseconds; tolerate seconds in fixtures.
        if timestamp >= 100_000_000_000:
            timestamp //= 1000
        if timestamp < 0:
            raise ValueError
    except (KeyError, TypeError, ValueError, OverflowError):
        raise FeishuError("飞书消息创建时间格式异常，已停止同步。") from None
    if not isinstance(message_id, str) or not message_id:
        raise FeishuError("飞书消息缺少消息 ID，已停止同步。")
    sender = item.get("sender") if isinstance(item.get("sender"), dict) else {}
    sender_id = str(sender.get("id") or "unknown")
    sender_name = str(sender.get("sender_name") or sender.get("name") or sender_id)
    return {"message_id": message_id, "chat_id": chat_id, "sender_id": sender_id,
            "sender_name": sender_name, "text": text.strip(), "created_at": timestamp,
            "message_type": message_type}
