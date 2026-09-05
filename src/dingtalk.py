"""
钉钉群机器人推送模块
=====================
支持两种安全模式：
1. 普通模式：直接 POST 到 webhook URL
2. 加签模式（推荐）：需额外配置 secret 密钥，更安全

钉钉机器人配置步骤：
1. 钉钉群 → 群设置 → 智能群助手 → 添加机器人 → 自定义
2. 安全设置：选「加签」或「自定义关键词」
3. 复制 webhook URL（形如 https://oapi.dingtalk.com/robot/send?access_token=xxx）
4. 如选加签，复制加签密钥（SEC 开头）

限制：
- 每分钟 20 条
- 单条消息 ≤ 20 KB
- 自定义关键词必须出现在推送内容中（默认「主升浪」）
"""
import os
import time
import hmac
import hashlib
import base64
import urllib.parse
import requests
from typing import Dict, Tuple


DINGTALK_API_TIMEOUT = 10  # 秒


def _sign_with_secret(secret: str) -> Tuple[str, str]:
    """
    生成加签模式的签名

    参数:
        secret: 加签密钥（SEC 开头）

    返回:
        (timestamp_str, sign_str)
    """
    timestamp = str(round(time.time() * 1000))
    secret_enc = secret.encode('utf-8')
    string_to_sign = f'{timestamp}\n{secret}'.encode('utf-8')
    hmac_code = hmac.new(
        secret_enc, string_to_sign, digestmod=hashlib.sha256
    ).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    return timestamp, sign


def push_to_dingtalk(webhook: str, payload: Dict, secret: str = '') -> Tuple[bool, str]:
    """
    推送消息到钉钉群

    参数:
        webhook: 钉钉 webhook URL（必填）
        payload: 消息内容（Dict），格式：
            {
                "msgtype": "markdown",
                "markdown": {
                    "title": "消息标题",
                    "text": "Markdown 文本"
                }
            }
        secret: 加签密钥（可选，配置加签时必填）

    返回:
        (success: bool, message: str)
    """
    if not webhook:
        return False, "未配置 webhook URL"

    # 加签模式：追加 timestamp 和 sign 到 URL
    url = webhook
    if secret:
        try:
            timestamp, sign = _sign_with_secret(secret)
            url = f'{webhook}&timestamp={timestamp}&sign={sign}'
        except Exception as e:
            return False, f"加签失败: {e}"

    # 消息大小检查（钉钉限制 20 KB）
    import json
    payload_size = len(json.dumps(payload).encode('utf-8'))
    if payload_size > 20 * 1024:
        return False, f"消息过大（{payload_size} bytes > 20 KB），请精简内容"

    try:
        resp = requests.post(
            url,
            json=payload,
            headers={'Content-Type': 'application/json; charset=utf-8'},
            timeout=DINGTALK_API_TIMEOUT,
        )
        resp.raise_for_status()
        result = resp.json()

        # 钉钉返回：{"errcode": 0, "errmsg": "ok"}
        if result.get('errcode') == 0:
            return True, "推送成功"
        else:
            errmsg = result.get('errmsg', '未知错误')
            return False, f"钉钉返回错误: {errmsg} (errcode={result.get('errcode')})"

    except requests.exceptions.Timeout:
        return False, "钉钉请求超时"
    except requests.exceptions.RequestException as e:
        return False, f"钉钉网络异常: {e}"
    except Exception as e:
        return False, f"推送失败: {e}"


def main():
    """本地测试入口"""
    webhook = os.environ.get('DINGTALK_WEBHOOK', '')
    secret = os.environ.get('DINGTALK_SECRET', '')

    if not webhook:
        print('❌ 请先设置 DINGTALK_WEBHOOK 环境变量')
        return

    test_payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": "主升浪日报 - 测试推送",
            "text": """# 🎯 测试推送

如果您看到这条消息，说明 **钉钉推送链路** 全部打通 ✅

之后每个交易日 **08:45 左右**，会在这里收到主升浪日报推送。
"""
        }
    }
    success, msg = push_to_dingtalk(webhook, test_payload, secret)
    print(f"{'✅' if success else '❌'} {msg}")


if __name__ == '__main__':
    main()