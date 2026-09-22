"""
持仓监控主入口（手动触发 / 应急用）
==========================
- 读取 portfolio.json 中的活跃持仓
- 拉取每只票的最新价（含当日最高/最低）
- 计算盈亏 %
- 判断是否触发止损 / 移动止盈
- 生成钉钉告警消息（仅在触发时推送，避免噪音）
- 推送后更新 trail_active / trail_high / closed 标志位

触发规则（2026-09-22 起「大波段」口径）：
- ① 固定止损：收盘/现价盈亏 ≤ stop_loss_pct（默认 -7%）  → 全平｜盘中**照常执行**
- ② 破 MA20：收盘价 < MA20                                  → 全平｜⚠️ 需**收盘确认**，盘中只提示
- ③ 移动止盈：盈利首破 +15% 启动，峰值回撤 8% 卖出          → 全平｜**默认停用**
- ④ 破 MA10 且未减半：收盘价 < MA10                         → 卖 50%｜⚠️ 需**收盘确认**，盘中只提示
- ⑤ 时间止损：僵尸/低效股                                    → 全平｜⚠️ 盘中只提示，盘后统一执行
- 警戒：盈利 ≥ +10%（接近启动线，给个温和提醒）

★ 盘中 vs 盘后的分工（协调一致）：
  - **事件型信号（固定止损）**：价格一旦触及即确凿 → 盘中照常执行，不等收盘。
  - **状态型信号（破 MA10 / 破 MA20 / 时间止损）**：以**收盘价**判定 → 盘中只提示，
    由 18:30 盘后复盘统一执行（那里才能配套补仓，并接受大盘弱势熔断约束）。
  - 均线取数为**上一交易日收盘**的 MA10/MA20（同一份均线面板），盘中用现价与之比较，
    仅作「即将破位」的预警，**不作为执行依据**。

⚠️ 常规止盈止损已合并到盘后复盘（main_review.py，18:30）。
   本脚本仅供盘中应急/调试手动触发，已取消定时调度。
   注：交易日钟（days_held）由盘后复盘推进，本脚本按当前计数评估、不做推进。
"""
import os
import sys
from pathlib import Path
from datetime import datetime
from typing import List, Dict

# 让脚本可以直接 python main_monitor.py 运行
sys.path.insert(0, str(Path(__file__).parent))

from src.portfolio import (
    get_active_holdings, mark_alerted, close_holding,
    evaluate_holding, update_trail_states, WARNING_PROFIT_PCT,
    TIME_STOP_REASONS, ZOMBIE_DAYS, ZOMBIE_PEAK_PCT,
    INEFFICIENT_DAYS, INEFFICIENT_PEAK_PCT,
    EXIT_USE_MA, EXIT_MA_HALF, EXIT_MA_CLEAR, TRAIL_ENABLED,
)
from src.data_fetcher import _init_tushare, fetch_ma_map
from src.notifier import push_all
from src.config import TEST_ONLY


# 说明：本脚本为「手动触发」的盘中监控工具。
# 常规止盈止损已合并到盘后复盘（main_review.py，18:30），此处仅作应急/调试用。


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


def build_alert_message(holding: Dict, evaluation: Dict, current_price: float) -> Dict:
    """生成单只持仓的告警钉钉消息 payload"""
    code = holding['code']
    name = holding['name']
    buy_price = holding['buy_price']
    trigger = evaluation['trigger']
    pnl_pct = evaluation['pnl_pct']
    pnl_amount = (current_price - buy_price) * 100  # 每 100 股盈亏（元）
    stop_pct = evaluation.get('stop_loss_pct', holding.get('stop_loss_pct'))
    activate_pct = evaluation.get('trail_activate_pct')
    drawdown_pct = evaluation.get('trail_drawdown_pct')
    peak = evaluation.get('trail_high')
    trigger_px = evaluation.get('trail_trigger_price')

    # 不同触发的 emoji + 标题
    if trigger == 'stop_loss':
        emoji = '🚨'
        title_emoji = '🚨'
        status_text = f'**已破止损线 {stop_pct}%**'
        advice = '⚠️ 建议立即决策：止损出局 / 持有观望（明确止损纪律）'
    elif trigger == 'take_profit':
        emoji = '🔒'
        title_emoji = '🔒'
        peak_txt = f'（峰值 +{evaluation.get("trail_peak_pnl", 0):.2f}%）' if peak else ''
        status_text = f'**移动止盈触发 → 卖出 {pnl_pct:+.2f}%**{peak_txt}'
        advice = f'💡 峰值回撤 {drawdown_pct}% 已触发，移动止盈落袋为安'
    elif trigger == 'ma20_exit':
        emoji = '🔻'
        title_emoji = '🔻'
        status_text = f'**盘中已跌破 MA{EXIT_MA_CLEAR}（{evaluation.get("ma20")}）**'
        advice = (f'💡 按离场规则，**收盘确认**跌破 MA{EXIT_MA_CLEAR} 即全部清仓。'
                  f'盘中仅提示，以 18:30 盘后收盘价为准；若尾盘拉回均线上方则不触发。')
    elif trigger == 'ma10_half':
        emoji = '✂️'
        title_emoji = '✂️'
        status_text = f'**盘中已跌破 MA{EXIT_MA_HALF}（{evaluation.get("ma10")}）**'
        advice = (f'💡 按离场规则，**收盘确认**跌破 MA{EXIT_MA_HALF} 即卖出 50%（减半后不补仓）。'
                  f'盘中仅提示，以 18:30 盘后收盘价为准。')
    elif trigger == 'warning':
        emoji = '⚡'
        title_emoji = '⚡'
        status_text = f'**接近移动止盈启动线（+{pnl_pct}%）**'
        advice = f'💡 距启动线 +{activate_pct}% 还差 {evaluation.get("distance_to_take_profit", 0):.2f}%，可提前规划'
    elif trigger in TIME_STOP_REASONS:
        days = evaluation.get('days_held')
        peak_pnl = evaluation.get('peak_high_pnl')
        peak_txt = f'{peak_pnl:+.2f}%' if peak_pnl is not None else '-'
        if trigger == 'time_stop_zombie':
            emoji = '🧟'
            title_emoji = '🧟'
            status_text = (f'**时间止损 · 僵尸股清理**（持有 {days} 交易日，'
                           f'期间最高仅 {peak_txt}，未达 {ZOMBIE_PEAK_PCT}%）')
            advice = (f'💡 该股持有 {ZOMBIE_DAYS} 个交易日仍未有效波动，判定为僵尸股，'
                      f'建议清仓、把仓位让给新的主升浪标的'
                      f'（盘中仅提示，实际清理由盘后复盘统一执行）')
        else:
            emoji = '🐌'
            title_emoji = '🐌'
            status_text = (f'**时间止损 · 低效股清理**（持有 {days} 交易日，'
                           f'期间最高仅 {peak_txt}，未达 {INEFFICIENT_PEAK_PCT}%）')
            advice = (f'💡 该股持有 {INEFFICIENT_DAYS} 个交易日仍未启动移动止盈，'
                      f'判定为低效股，建议清仓换股'
                      f'（盘中仅提示，实际清理由盘后复盘统一执行）')
    else:
        # 正常状态，不应该推送（除非是 summary）
        return None

    title = f'{title_emoji} 持仓监控告警 · {name}({code})'

    trail_line = ''
    if TRAIL_ENABLED and evaluation.get('trail_active') and peak and trigger_px:
        trail_line = (f'\n- 移动止盈：已启动 ｜ 峰值 {peak:.2f} ｜ 回撤线 {trigger_px:.2f}'
                      f'（回撤 {evaluation.get("trail_drawdown_pct")}% 卖出）')

    # 均线离场距离（MA 模式下的主参考线；均线为**上一交易日收盘**口径）
    ma_line = ''
    if EXIT_USE_MA:
        g10, g20 = evaluation.get('ma10_gap_pct'), evaluation.get('ma20_gap_pct')
        if g10 is not None and g20 is not None:
            ma_line = (f'\n- 距 MA{EXIT_MA_HALF} 减半线：**{g10:+.2f}%**'
                       f'（MA{EXIT_MA_HALF} = {evaluation.get("ma10")}）'
                       f'\n- 距 MA{EXIT_MA_CLEAR} 清仓线：**{g20:+.2f}%**'
                       f'（MA{EXIT_MA_CLEAR} = {evaluation.get("ma20")}）')
        elif g10 is not None:
            ma_line = (f'\n- 距 MA{EXIT_MA_HALF} 减半线：**{g10:+.2f}%**'
                       f'（MA{EXIT_MA_HALF} = {evaluation.get("ma10")}）')
        else:
            ma_line = f'\n- ⚠️ 均线数据缺失（次新股）→ 本日仅固定止损生效'
    else:
        ma_line = f'\n- 距启动/回撤线：还差 {evaluation.get("distance_to_take_profit", 0):.2f}%{trail_line}'

    half_line = ''
    if evaluation.get('half_sold') and evaluation.get('half_sold_pnl') is not None:
        _rem = int((evaluation.get('remaining_ratio') or 0.5) * 100)
        half_line = (f'\n- ◐ 已减半：半仓段 **{evaluation["half_sold_pnl"]:+.2f}%**'
                     f'（{evaluation.get("half_sold_price")}）｜剩余 {_rem}% 继续持有')

    text = f"""## {title}

**{name} ({code})**

| 项目 | 数值 |
|---|---|
| 买入价 | {buy_price:.2f} 元 |
| 现价 | {current_price:.2f} 元 |
| 浮动盈亏 | **{pnl_pct:+.2f}%** |
| 每 100 股盈亏 | **{pnl_amount:+.2f} 元** |
| 状态 | {status_text} |

- 距止损线 ({stop_pct}%)：还差 {evaluation['distance_to_stop_loss']:.2f}%{ma_line}{half_line}

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
    _dcol = f'距MA{EXIT_MA_HALF}/MA{EXIT_MA_CLEAR}' if EXIT_USE_MA else '距启动/回撤'
    lines.append(f'| # | 名称(代码) | 买入价 | 现价 | 盈亏% | 距止损 | {_dcol} | 状态 |')
    lines.append('|---|---|---|---|---|---|---|---|')

    for i, (h, e) in enumerate(zip(holdings, evaluations), 1):
        if e is None:
            continue
        # 状态 emoji
        if e['trigger'] == 'stop_loss':
            status = '🚨 固定止损'
        elif e['trigger'] == 'ma20_exit':
            status = f'🔻 破MA{EXIT_MA_CLEAR}（待收盘确认）'
        elif e['trigger'] == 'ma10_half':
            status = f'✂️ 破MA{EXIT_MA_HALF}（待收盘确认）'
        elif e.get('fused'):
            status = '⚡ 熔断暂缓'
        elif e['trigger'] == 'take_profit':
            status = '🔒 移动止盈卖出'
        elif e['trigger'] == 'time_stop_zombie':
            status = '🧟 僵尸股清理'
        elif e['trigger'] == 'time_stop_inefficient':
            status = '🐌 低效股清理'
        elif e.get('half_sold'):
            status = f'◐ 半仓持有（已破MA{EXIT_MA_HALF}）'
        elif e.get('trail_active'):
            status = f"🔒 移动止盈中(峰{e.get('trail_peak_pnl', 0):+.0f}%)"
        elif e.get('time_stop_hint'):
            status = e['time_stop_hint']
        elif e['trigger'] == 'warning':
            status = '⚡ 接近启动'
        else:
            status = '🟢 正常'
        if EXIT_USE_MA:
            g10, g20 = e.get('ma10_gap_pct'), e.get('ma20_gap_pct')
            dist_txt = (f"{(f'{g10:+.1f}%' if g10 is not None else '-')}"
                        f"/{(f'{g20:+.1f}%' if g20 is not None else '-')}")
        else:
            dist = e.get('distance_to_take_profit')
            dist_txt = f'{dist:.1f}%' if dist is not None else '-'
        lines.append(
            f"| {i} | {h['name']}({h['code']}) | {h['buy_price']:.2f} | "
            f"{e.get('current_price', 0):.2f} | **{e['pnl_pct']:+.2f}%** | "
            f"{e['distance_to_stop_loss']:.1f}% | {dist_txt} | {status} |"
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


# ★ 盘中只执行「事件型」保护性出场（固定止损；若启用移动止盈也含其触发）。
#   「状态型」闸门（破 MA10 减半 / 破 MA20 清仓 / 时间止损）需**收盘价确认**，
#   盘中一律只提示、不执行 —— 它们统一交给盘后复盘（那里能配套补仓、并受熔断约束，
#   也避免盘中误判「假破位」后半个交易日空仓）。
EXIT_TRIGGERS = ('stop_loss', 'take_profit')

# 盘中需要「只提示、不执行」的触发原因（状态型）
ALERT_ONLY_TRIGGERS = ('ma10_half', 'ma20_exit') + TIME_STOP_REASONS


def _exit_peak(e: Dict):
    """平仓时的峰值口径：移动止盈用 trail 峰值，其余（止损/破MA20）用期间最高涨幅"""
    return e.get('trail_peak_pnl') if e.get('trigger') == 'take_profit' else e.get('peak_high_pnl')


def _close_from_trigger(holding: Dict, e: Dict) -> None:
    """按触发结果清仓（**仅盘中执行类**：固定止损 / 移动止盈触发）
    ⚠️ 破MA10/破MA20 属状态型（需收盘确认），不在 EXIT_TRIGGERS 内，此函数不会被调用。"""
    close_holding(holding['code'],
                  e['exit_pnl_pct'] if e['exit_pnl_pct'] is not None else e['pnl_pct'],
                  reason=e['trigger'],
                  price=e['exit_price'] or e.get('current_price'),
                  peak_pnl=_exit_peak(e),
                  days_held=e.get('days_held'))


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
    # ⚠️ 均线取数来自「同一份均线面板」（上一交易日收盘口径）→ 零额外调用；
    #    盘中用现价与之比较，仅作「即将破位」预警，**执行以盘后收盘价为准**。
    try:
        ma_map = fetch_ma_map(codes) if EXIT_USE_MA else {}
    except Exception as _e:
        ma_map = {}
        print(f'  ⚠️ 均线取数失败（盘中仅按止损/其余闸门评估）: {_e}')
    if EXIT_USE_MA:
        _hit = sum(1 for c in codes if (ma_map.get(c) or {}).get('ma20') is not None)
        print(f'  📐 离场均线：{_hit}/{len(codes)} 只含 MA{EXIT_MA_CLEAR}' if ma_map
              else '  📐 离场均线：未取到（本日按止损/其余闸门评估）')

    evaluations = []
    alerts = []
    trail_updates = []
    for h in holdings:
        info = prices.get(h['code'])
        if not info or info['price'] <= 0:
            print(f'  ⏭️ {h["name"]}({h["code"]}) 无行情数据')
            continue
        _m = ma_map.get(h['code']) or {}
        eval_result = evaluate_holding(h, info['price'],
                                       day_high=info.get('high'), day_low=info.get('low'),
                                       ma10=_m.get('ma10'), ma20=_m.get('ma20'))
        if eval_result is None:
            continue
        eval_result['current_price'] = info['price']
        evaluations.append((h, eval_result))
        trail_updates.append({'code': h['code'],
                              'trail_active': eval_result['trail_active'],
                              'trail_high': eval_result['trail_high']})

        # 检查是否需要告警（且未告警过）
        if (eval_result['trigger'] in ('stop_loss', 'take_profit', 'warning')
                or eval_result['trigger'] in ALERT_ONLY_TRIGGERS) and not h.get('alerted'):
            payload = build_alert_message(h, eval_result, info['price'])
            if payload:
                alerts.append((h, eval_result, payload))

    # 落盘移动止盈状态（峰值抬升 / 新启动）
    update_trail_states(trail_updates)

    # 推送告警（每个持仓单独推送，钉钉 + 飞书 双通道）
    if alerts and not TEST_ONLY:
        print(f'\n📤 准备推送 {len(alerts)} 条告警...')
        for h, e, payload in alerts:
            try:
                results = push_all(payload)
                detail = ' '.join(
                    ('✅' if ok else '❌') + ch for ch, (ok, _) in results.items())
                if any(ok for ok, _ in results.values()):
                    print(f'  ✅ {h["name"]}({h["code"]}) 告警已推送 [{detail}]')
                    # 推送成功后更新状态
                    mark_alerted(h['code'])
                    if e['trigger'] in EXIT_TRIGGERS:
                        _close_from_trigger(h, e)
                        print(f'     → 已标记为出场 ({e["trigger"]})')
                else:
                    errs = '; '.join(
                        f'{ch}: {msg}' for ch, (ok, msg) in results.items() if not ok)
                    print(f'  ❌ {h["name"]}({h["code"]}) 推送失败: {errs}')
            except Exception as ex:
                print(f'  ❌ 推送异常: {ex}')
    elif alerts and TEST_ONLY:
        print(f'\n🧪 TEST_ONLY 模式：模拟推送 {len(alerts)} 条告警')
        for h, e, payload in alerts:
            print(f'  📋 {h["name"]}({h["code"]}) trigger={e["trigger"]} pnl={e["pnl_pct"]:+.2f}%')
            print(f'  📝 Title: {payload["markdown"]["title"]}')
            mark_alerted(h['code'])
            if e['trigger'] in EXIT_TRIGGERS:
                _close_from_trigger(h, e)
    else:
        print('\n✅ 持仓正常，无触发告警')

    print('\n🎉 持仓监控完成')


if __name__ == '__main__':
    main()