"""
飞书消息推送模块
"""

import json
import requests
from config import FEISHU_WEBHOOK_URL

# 信号颜色映射
SIGNAL_COLORS = {
    'long': 'green',
    'short': 'red',
    'neutral': 'blue',
}

DIRECTION_LABEL = {
    'long': '做多 🟢',
    'short': '做空 🔴',
    'neutral': '关注 👀',
}


def send_signal(symbol: str, signal_label: str, direction: str,
                price: float, timestamp_str: str, interval: str = "15"):
    """
    发送交易信号到飞书群

    Args:
        symbol: 交易对名称
        signal_label: 信号中文描述
        direction: 'long' / 'short' / 'neutral'
        price: 触发价格
        timestamp_str: 时间字符串
        interval: K线周期（分钟）
    """
    color = SIGNAL_COLORS.get(direction, 'blue')
    direction_text = DIRECTION_LABEL.get(direction, direction)
    emoji = {'long': '🟢', 'short': '🔴', 'neutral': '🔵'}.get(direction, 'ℹ️')

    card = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"{emoji} 交易信号 · {symbol}"
                },
                "template": color
            },
            "elements": [
                {
                    "tag": "div",
                    "fields": [
                        {
                            "is_short": True,
                            "text": {"tag": "lark_md", "content": f"**📊 信号**\n{signal_label}"}
                        },
                        {
                            "is_short": True,
                            "text": {"tag": "lark_md", "content": f"**🎯 方向**\n{direction_text}"}
                        },
                        {
                            "is_short": True,
                            "text": {"tag": "lark_md", "content": f"**💰 价格**\n{price:.4f}"}
                        },
                        {
                            "is_short": True,
                            "text": {"tag": "lark_md", "content": f"**⏱ 周期**\n{interval}分钟"}
                        }
                    ]
                },
                {
                    "tag": "hr"
                },
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"⏰ 触发时间：{timestamp_str}\n\n⚠️ **请评估开单时机，注意风险管理**"
                    }
                }
            ]
        }
    }

    try:
        resp = requests.post(
            FEISHU_WEBHOOK_URL,
            json=card,
            headers={"Content-Type": "application/json"},
            timeout=10
        )
        result = resp.json()
        if result.get("code") == 0 or result.get("StatusCode") == 0:
            print(f"  ✅ 飞书推送成功: {signal_label}")
            return True
        else:
            print(f"  ❌ 飞书推送失败: {result}")
            return False
    except Exception as e:
        print(f"  ❌ 飞书推送异常: {e}")
        return False
