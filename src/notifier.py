"""
统一推送调度器
===============
一个入口同时推到多个通道（钉钉 / 飞书），任一通道失败不影响其他通道。

【为什么需要】
早盘选股、盘后复盘、盘中监控三个入口都要推消息。如果每个入口自己写
「先推钉钉再推飞书」的循环，一旦某天要加通道（企业微信 / 邮件），
就要改三处。统一收口到这里，通道增删只改本文件。

【通道开关】
环境变量 PUSH_CHANNELS（逗号分隔），默认 'dingtalk,feishu'：
  - PUSH_CHANNELS=dingtalk          只推钉钉
  - PUSH_CHANNELS=feishu            只推飞书
  - PUSH_CHANNELS=dingtalk,feishu   双通道（默认）

未配置 webhook 的通道会自动跳过（记为 skipped，不算失败），
因此「只配了钉钉」的用户不会有任何报错。
"""
import os
from typing import Dict, Tuple, List

from src.dingtalk import push_to_dingtalk
from src.feishu import push_to_feishu


def _env(name: str, default: str = '') -> str:
    return os.environ.get(name, default).strip()


def get_channels() -> List[str]:
    """当前启用的通道列表（已去重、去空、按固定顺序）"""
    raw = _env('PUSH_CHANNELS', 'dingtalk,feishu').lower()
    wanted = [c.strip() for c in raw.split(',') if c.strip()]
    order = ['dingtalk', 'feishu']
    return [c for c in order if c in wanted]


def push_all(payload: Dict, payload_title: str = '',
             log=None) -> Dict[str, Tuple[bool, str]]:
    """把同一份 payload 推到所有启用的通道

    参数:
        payload: 钉钉格式 {"msgtype":"markdown","markdown":{"title":..,"text":..}}
        payload_title: 标题（便于日志/飞书主题色判断，留空则从 payload 取）
        log: 可选的 logger（有 info/warning/error 方法即可）

    返回:
        {通道名: (success, message)}
        - 未配置 webhook 的通道 → (True, '未配置 webhook，已跳过')
          （记为成功，避免因未启用通道而让整体判定为失败）
        - 通道内部异常 → (False, '通道异常: ...')
    """
    if not payload_title:
        payload_title = (payload or {}).get('markdown', {}).get('title', '')

    results: Dict[str, Tuple[bool, str]] = {}

    def _log(level: str, msg: str):
        if log is None:
            return
        fn = getattr(log, level, None) or getattr(log, 'info', None)
        if fn:
            try:
                fn(msg)
            except Exception:
                pass

    for ch in get_channels():
        try:
            if ch == 'dingtalk':
                hook = _env('DINGTALK_WEBHOOK')
                sec = _env('DINGTALK_SECRET')
                if not hook:
                    results[ch] = (True, '未配置 webhook，已跳过')
                    _log('info', '⏭️ 钉钉未配置 webhook，跳过')
                    continue
                ok, msg = push_to_dingtalk(hook, payload, sec)
            elif ch == 'feishu':
                hook = _env('FEISHU_WEBHOOK')
                sec = _env('FEISHU_SECRET')
                if not hook:
                    results[ch] = (True, '未配置 webhook，已跳过')
                    _log('info', '⏭️ 飞书未配置 webhook，跳过')
                    continue
                ok, msg = push_to_feishu(hook, payload, sec)
            else:
                results[ch] = (False, f'未知通道: {ch}')
                _log('warning', f'⚠️ 未知推送通道: {ch}')
                continue

            results[ch] = (ok, msg)
            if ok:
                _log('info', f'✅ {ch} 推送成功：{msg}')
            else:
                _log('error', f'❌ {ch} 推送失败：{msg}')
        except Exception as e:                     # 单通道异常不影响其他通道
            results[ch] = (False, f'通道异常: {e}')
            _log('error', f'❌ {ch} 推送异常：{e}')

    return results


def summarize(results: Dict[str, Tuple[bool, str]]) -> str:
    """把 push_all 的结果压成一行，便于控制台输出"""
    if not results:
        return '无可用推送通道'
    parts = []
    for ch, (ok, msg) in results.items():
        icon = '✅' if ok else '❌'
        parts.append(f'{icon}{ch}')
    return ' | '.join(parts)
