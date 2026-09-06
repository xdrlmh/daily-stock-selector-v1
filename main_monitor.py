"""
持仓监控主入口
==========================
- 读取 portfolio.json 中的活跃持仓
- 拉取每只票的最新价
- 计算盈亏 %
- 判断是否触发止盈/止损
- 生成钉钉告警消息（仅在触发时推送，避免噪音）
- 推送后更新 alerted 标志位 / closed 标志位

触发规则：
- 止损：盈亏 ≤ stop_loss_pct（默认 -7%）
- 止盈：盈亏 ≥ take_profit_pct（默认 +15%）
- 警戒：盈亏 ≥ +10%（接近止盈，给个温和提醒）

每个交易日 09:30-15:00 每 30 分钟跑一次（cron 控制）
"""
import os
import sys
from pathlib import Path
from datetime import datetime
from typing import List, Dict

# 让脚本可以直接 python main_monitor.py 运行
sys.path.insert(0, str(Path(__file__).parent))

from src.portfolio import (
    load_portfolio, get_active_holdings,
    update_holding_alerted, close_holding
)
from src.data_fetcher import _init_tushare
from src.dingtalk import push_to_dingtalk
from src.config import DINGTALK_WEBHOOK, DINGTALK_SECRET, TEST_ONLY


# ============= 警戒阈值（仅预警，不自动平仓）=============
WARNING_PROFIT_PCT = 10.0   # 接近止盈的预警阈值


def fetch_latest_prices(pro, codes: List[str]) -> Dict[str, Dict]:
    """
    批量拉取持仓股的最新行情。
    返回 {code: {'price': 现价, 'pct_change': 当日涨幅, 'high': 最高, 'low': 最低}}
    """
    if not codes:
        return {}

    # Tushare 的 daily_basic + daily 都可以查；这里用 daily_basic 更轻
    today = datetime.now().strftime('%Y%m%d')

    result = {}
    try:
        # 先尝试今日实时（盘中）
        df = pro.daily_basic(
            trade_date=today,
            ts_code=','.join([f'{c}.{"SH" if c.startswith("6") else "SZ"}' for c in codes]),
            fields='ts_code,close,pct_chg,high,low,vol'
        )
        if df is not None and not df.empty:
            for _, row in df.iterrows():
                code = row['ts_code'].split('.')[0]
                result[code] = {
                    'price': float(row['close']) if row['close'] else 0,
                    'pct_change': float(row['pct_chg']) if row['pct_chg'] else 0,
                    'high': float(row['high']) if row['high'] else 0,
                    'low': float(row['low']) if row['low'] else 0,
                }
    except Exception as e:
        print(f'⚠️ 拉取今日行情失败: {e}')

    # 如果今日没有数据（盘前/中午），尝试最近一个交易日
    if not result:
        try:
            from datetime import timedelta
            for days_back in range(1, 10):
                check_date = (datetime.now() - timedelta(days=days_back)).strftime('%Y%m%d')
                df = pro.daily_basic(
                    trade_date=check_date,
                    ts_code=','.join([f'{c}.{"SH" if c.startswith("6") else "SZ"}' for c in codes]),
                    fields='ts_code,close,pct_chg,high,low'
                )
                if df is not None and not df.empty:
                    print(f'  ℹ️ 使用 {check_date} 数据（非交易日）')
                    for _, row in df.iterrows():
                        code = row['ts_code'].split('.')[0]
                        result[code] = {
                            'price': float(row['close']) if row['close'] else 0,
                            'pct_change': float(row['pct_chg']) if row['pct_chg'] else 0,
                            'high': float(row['high']) if row['high'] else 0,
                            'low': float(row['low']) if row['low'] else 0,
                        }
                    break
        except Exception as e:
            print(f'⚠️ 拉取历史行情也失败: {e}')

    return result


def evaluate_holding(holding: Dict, current_price: float) -> Dict:
    """
    评估单只持仓的状态。
    返回 {
        'pnl_pct': 盈亏 %,
        'pnl_amount': 每手盈亏金额（按 100 股估算）,
        'trigger': 'stop_loss' | 'take_profit' | 'warning' | 'normal',
        'distance_to_stop_loss': 距止损线 %,
        'distance_to_take_profit': 距止盈线 %,
    }
    """
    buy_price = holding['buy_price']
    if current_price <= 0 or buy_price <= 0:
        return None

    pnl_pct = (current_price / buy_price - 1) * 100
    pnl_amount_per_100 = (current_price - buy_price) * 100  # 每 100 股盈亏

    distance_to_stop = pnl_pct - holding['stop_loss_pct']      # 距止损还有多少
    distance_to_take = holding['take_profit_pct'] - pnl_pct     # 距止盈还有多少

    # 判断触发类型（按优先级）
    # 注意：阈值判断用严格边界，pnl_pct == take_profit_pct 不算触发（要给容差）
    if pnl_pct <= holding['stop_loss_pct']:
        trigger = 'stop_loss'
    elif pnl_pct > holding['take_profit_pct']:
        trigger = 'take_profit'
    elif pnl_pct >= WARNING_PROFIT_PCT and not holding.get('alerted'):
        trigger = 'warning'
    else:
        trigger = 'normal'

    return {
        'pnl_pct': round(pnl_pct, 2),
        'pnl_amount_per_100': round(pnl_amount_per_100, 2),
        'trigger': trigger,
        'distance_to_stop_loss': round(distance_to_stop, 2),
        'distance_to_take_profit': round(distance_to_take, 2),
    }


def build_alert_message(holding: Dict, evaluation: Dict, current_price: float) -> Dict:
    """生成单只持仓的告警钉钉消息 payload"""
    code = holding['code']
    name = holding['name']
    buy_price = holding['buy_price']
    trigger = evaluation['trigger']
    pnl_pct = evaluation['pnl_pct']
    pnl_amount = evaluation['pnl_amount_per_100']
    stop_pct = holding['stop_loss_pct']
    take_pct = holding['take_profit_pct']

    # 不同触发的 emoji + 标题
    if trigger == 'stop_loss':
        emoji = '🚨'
        title_emoji = '🚨'
        status_text = f'**已破止损线 {stop_pct}%**'
        advice = f'⚠️ 建议立即决策：止损出局 / 持有观望（明确止损纪律）'
    elif trigger == 'take_profit':
        emoji = '🎯'
        title_emoji = '🎯'
        status_text = f'**已达止盈线 +{take_pct}%**'
        advice = f'💡 建议分批止盈：可考虑先减半仓，剩余博更高（注意回撤）'
    elif trigger == 'warning':
        emoji = '⚡'
        title_emoji = '⚡'
        status_text = f'**接近止盈（+{pnl_pct}%）**'
        advice = f'💡 接近止盈线 {take_pct}%，可开始关注分批止盈计划'
    else:
        # 正常状态，不应该推送（除非是 summary）
        return None

    title = f'{title_emoji} 持仓监控告警 · {name}({code})'

    text = f"""## {title}

**{name} ({code})**

| 项目 | 数值 |
|---|---|
| 买入价 | {buy_price:.2f} 元 |
| 现价 | {current_price:.2f} 元 |
| 浮动盈亏 | **{pnl_pct:+.2f}%** |
| 每 100 股盈亏 | **{pnl_amount:+.2f} 元** |
| 状态 | {status_text} |

- 距止损线 ({stop_pct}%)：还差 {evaluation['distance_to_stop_loss']:.2f}%
- 距止盈线 (+{take_pct}%)：还差 {evaluation['distance_to_take_profit']:.2f}%

{advice}

⏰ {datetime.now().strftime('%Y-%m-%d %H:%M')}
"""

    return {
        'msgtype': 'markdown',
        'markdown': {
            'title': title,
            'text': text
        }
    }


def build_summary_message(holdings: List[Dict], evaluations: List[Dict]) -> Dict:
    """生成持仓整体汇总（每次监控都推送，无触发时给个简版）"""
    if not holdings:
        return None

    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    lines = [f'## 📊 持仓监控汇总 · {now}\n']
    lines.append(f'**共监控 {len(holdings)} 只持仓**\n')
    lines.append('| # | 名称(代码) | 买入价 | 现价 | 盈亏% | 距止损 | 距止盈 | 状态 |')
    lines.append('|---|---|---|---|---|---|---|---|')

    for i, (h, e) in enumerate(zip(holdings, evaluations), 1):
        if e is None:
            continue
        # 状态 emoji
        if e['trigger'] == 'stop_loss':
            status = '🚨 止损'
        elif e['trigger'] == 'take_profit':
            status = '🎯 止盈'
        elif e['trigger'] == 'warning':
            status = '⚡ 警戒'
        else:
            status = '🟢 正常'
        lines.append(
            f"| {i} | {h['name']}({h['code']}) | {h['buy_price']:.2f} | "
            f"{e.get('current_price', 0):.2f} | **{e['pnl_pct']:+.2f}%** | "
            f"{e['distance_to_stop_loss']:.1f}% | {e['distance_to_take_profit']:.1f}% | {status} |"
        )

    text = '\n'.join(lines)
    title = f'📊 持仓监控 · {len(holdings)}只 · {now.split()[1]}'

    return {
        'msgtype': 'markdown',
        'markdown': {
            'title': title,
            'text': text
        }
    }


def main():
    print(f'\n🚨 持仓监控启动 · {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')

    # 加载持仓池
    holdings = get_active_holdings()
    if not holdings:
        print('📭 当前无活跃持仓，跳过')
        return

    print(f'📂 加载到 {len(holdings)} 只持仓：{[h["name"] for h in holdings]}')

    # 拉取最新行情
    pro = _init_tushare()
    codes = [h['code'] for h in holdings]
    prices = fetch_latest_prices(pro, codes)
    if not prices:
        print('⚠️ 未获取到任何行情，跳过本次推送')
        return

    # 评估每只持仓
    evaluations = []
    alerts = []
    for h in holdings:
        info = prices.get(h['code'])
        if not info or info['price'] <= 0:
            print(f'  ⏭️ {h["name"]}({h["code"]}) 无行情数据')
            continue
        eval_result = evaluate_holding(h, info['price'])
        if eval_result is None:
            continue
        eval_result['current_price'] = info['price']
        evaluations.append((h, eval_result))

        # 检查是否需要告警（且未告警过）
        if eval_result['trigger'] in ('stop_loss', 'take_profit', 'warning') and not h.get('alerted'):
            payload = build_alert_message(h, eval_result, info['price'])
            if payload:
                alerts.append((h, eval_result, payload))

    # 推送告警（每个持仓单独推送）
    if alerts and not TEST_ONLY:
        print(f'\n📤 准备推送 {len(alerts)} 条告警...')
        webhook = DINGTALK_WEBHOOK
        secret = DINGTALK_SECRET
        for h, e, payload in alerts:
            try:
                ok, msg = push_to_dingtalk(webhook, payload, secret)
                if ok:
                    print(f'  ✅ {h["name"]}({h["code"]}) 告警已推送')
                    # 推送成功后更新状态
                    update_holding_alerted(h['code'])
                    if e['trigger'] == 'stop_loss' or e['trigger'] == 'take_profit':
                        close_holding(h['code'], e['pnl_pct'])
                        print(f'     → 已标记为出场 ({e["trigger"]})')
                else:
                    print(f'  ❌ {h["name"]}({h["code"]}) 推送失败: {msg}')
            except Exception as ex:
                print(f'  ❌ 推送异常: {ex}')
    elif alerts and TEST_ONLY:
        print(f'\n🧪 TEST_ONLY 模式：模拟推送 {len(alerts)} 条告警')
        for h, e, payload in alerts:
            print(f'  📋 {h["name"]}({h["code"]}) trigger={e["trigger"]} pnl={e["pnl_pct"]:+.2f}%')
            print(f'  📝 Title: {payload["markdown"]["title"]}')
            update_holding_alerted(h['code'])
            if e['trigger'] in ('stop_loss', 'take_profit'):
                close_holding(h['code'], e['pnl_pct'])
    else:
        print('\n✅ 持仓正常，无触发告警')

    print('\n🎉 持仓监控完成')


if __name__ == '__main__':
    main()