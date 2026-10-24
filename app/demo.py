from datetime import datetime, timedelta
from .periods import SHANGHAI, timestamp

CHATS = [
    {"chat_id": "demo-product", "name": "示例 · 产品研发", "description": "演示数据：需求、开发与上线进度"},
    {"chat_id": "demo-project", "name": "示例 · 项目协作", "description": "演示数据：项目里程碑与风险同步"},
]


def seed_demo(storage, today=None):
    today = today or datetime.now(SHANGHAI).date()
    templates = [
        ("demo-product", "demo-me", "示例用户 · 林晨", "已完成客户列表搜索功能，补齐空状态说明，等待产品验收。"),
        ("demo-product", "demo-colleague", "示例同事 · 陈悦", "正在联调客户详情接口，今天继续核对分页和权限边界。"),
        ("demo-project", "demo-me", "示例用户 · 林晨", "风险：测试环境的回调配置尚未开通，需要管理员协助，可能影响本周验收。"),
        ("demo-project", "demo-me", "示例用户 · 林晨", "下一步：明天补充异常场景测试，并同步验收清单。"),
    ]
    messages = []
    for offset in range(35):
        day = today - timedelta(days=offset)
        # Dates include today so daily / weekly / monthly previews all work immediately.
        for index, (chat_id, sender_id, sender_name, text) in enumerate(templates):
            messages.append({"message_id": f"demo-{day.isoformat()}-{index}", "chat_id": chat_id,
                             "sender_id": sender_id, "sender_name": sender_name,
                             "text": f"【模拟工作记录 {day:%m/%d}】{text}",
                             "created_at": timestamp(day) + (9 + index * 2) * 3600,
                             "message_type": "text"})
    storage.save_messages("demo", messages, CHATS,
                          coverage=([c["chat_id"] for c in CHATS], timestamp(today - timedelta(days=34)), timestamp(today + timedelta(days=1))))
    return len(messages)
