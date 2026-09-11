#!/usr/bin/env python3
"""
主升浪盘后复盘 - 主入口（钉钉群推送版）
=====================================
执行流程（每交易日 18:30 推送）：
1. 抓取全市场行情 + 资金流（数据未发布/读取失败时自动回退前一交易日）
2. 抓取大盘复盘数据（指数/主力/趋势/成交额/涨停跌停）
3. 💼 模拟盘持仓结算（原「持仓止盈止损监控」已合并到此）：
   - 待入场持仓按「当日开盘价」归一化买入成本
   - 按当日收盘价评估盈亏 → 触发止盈/止损则平仓
   - 平仓后自动补仓（用当日强势股 TOP 补齐，最多 3 只）
4. 今日强势股 TOP5（五维评分，收盘后数据）
5. 板块温度 TOP3（按题材聚合力强板块）
6. 生成钉钉复盘 payload + 保存完整复盘报告 → 一次推送

与早盘 main.py 互补：早盘看「今天关注什么」，盘后看「今天发生了什么、持仓怎么办、明天怎么看」。
"""
import sys
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.config import DINGTALK_WEBHOOK, DINGTALK_SECRET, TEST_ONLY, REPORTS_DIR, validate_config
from src.data_fetcher import (
    fetch_market_spot, fetch_fund_flow_rank,
    filter_main_board, enrich_with_fund_flow,
    fetch_market_review,
)
from src.selector import screen_stocks, analyze_sector_heat
from src.report import generate_review_payload, save_review_report
from src.dingtalk import push_to_dingtalk
from src.portfolio import (
    MAX_HOLDINGS, DEFAULT_STOP_LOSS_PCT, DEFAULT_TAKE_PROFIT_PCT,
    get_active_holdings, evaluate_holding, close_holding, mark_alerted,
    normalize_pending_entries, fill_portfolio_from_candidates,
    portfolio_stats,
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger('market-review')


def build_price_map(spot_df: pd.DataFrame) -> dict:
    """从全市场行情构建 {code: {'open': 开盘价, 'price': 收盘价}}"""
    price_map = {}
    if spot_df is None or spot_df.empty:
        return price_map
    for _, row in spot_df.iterrows():
        code = str(row['code']).zfill(6)
        open_px = row.get('open')
        close_px = row.get('price')
        price_map[code] = {
            'open': float(open_px) if pd.notna(open_px) else 0.0,
            'price': float(close_px) if pd.notna(close_px) else 0.0,
        }
    return price_map


def settle_holdings(price_map: dict, data_date: str):
    """
    持仓结算（合并自原持仓监控）：
    1. 待入场持仓按当日开盘价归一化买入成本；
    2. 按当日收盘价评估盈亏，触发止盈/止损则平仓；
    3. 「接近止盈」标记已提醒，避免重复。

    返回 (holdings_status, closed_today, price_changes)
    """
    # 1) 归一化待入场价（信号日收盘价 → 实际建仓日开盘价）
    price_changes = normalize_pending_entries(price_map, data_date)

    holdings = get_active_holdings()
    holdings_status, closed_today = [], []
    if not holdings:
        return holdings_status, closed_today, price_changes

    print(f'💼 持仓结算：共 {len(holdings)} 只活跃持仓')
    for h in holdings:
        info = price_map.get(h['code'])
        cur = info['price'] if info else 0.0

        # 尚无有效行情 → 占位展示
        if not info or cur <= 0:
            holdings_status.append({
                'code': h['code'], 'name': h.get('name', ''),
                'buy_price': float(h.get('buy_price') or 0), 'current_price': 0.0,
                'pnl_pct': 0.0, 'trigger': 'no_quote', 'status_text': '❓ 无行情',
                'distance_to_stop_loss': 0.0, 'distance_to_take_profit': 0.0,
                'hold_days': None, 'entry_pending': bool(h.get('entry_pending')),
            })
            continue

        # 待归一化的新仓：不计盈亏（实际建仓在次日开盘）
        if h.get('entry_pending'):
            holdings_status.append({
                'code': h['code'], 'name': h.get('name', ''),
                'buy_price': float(h.get('buy_price') or 0), 'current_price': cur,
                'pnl_pct': 0.0, 'trigger': 'pending', 'status_text': '⏳ 待开盘价',
                'distance_to_stop_loss': 0.0, 'distance_to_take_profit': 0.0,
                'hold_days': None, 'entry_pending': True,
            })
            continue

        ev = evaluate_holding(h, cur)
        if ev is None:
            continue

        if ev['trigger'] in ('stop_loss', 'take_profit'):
            closed = close_holding(h['code'], ev['pnl_pct'],
                                   reason=ev['trigger'], price=cur)
            if closed:
                closed_today.append(closed)
                print(f"  {'🎯' if ev['trigger'] == 'take_profit' else '🚨'} "
                      f"{h['name']}({h['code']}) {ev['trigger']} {ev['pnl_pct']:+.2f}% → 已平仓")
            continue  # 已平仓，不再计入活跃持仓表

        holdings_status.append(ev)
        if ev['trigger'] == 'warning':
            mark_alerted(h['code'])
            print(f"  ⚡ {h['name']}({h['code']}) 接近止盈 {ev['pnl_pct']:+.2f}%（已提醒）")

    return holdings_status, closed_today, price_changes


def main():
    print('=' * 60)
    print(f'📊 主升浪盘后复盘 启动 - {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    print('=' * 60)

    # 1. 校验配置
    try:
        validate_config()
        if TEST_ONLY:
            print('⚠️  TEST_ONLY 模式：不推送钉钉')
        else:
            print('📤 推送通道：钉钉群机器人')
    except ValueError as e:
        log.error(f'配置错误: {e}')
        sys.exit(1)

    # 2. 抓取数据（早盘同源；内部含「数据未发布 → 自动回退前一交易日」机制）
    spot_df = fetch_market_spot()
    if spot_df.empty:
        log.error('行情数据抓取失败（已尝试逐日回退），退出')
        sys.exit(1)

    fund_df = fetch_fund_flow_rank()

    # 3. 过滤主板非ST + 合并资金流
    main_board = filter_main_board(spot_df)
    log.info(f'主板非ST候选：{len(main_board)} 只')
    enriched = enrich_with_fund_flow(main_board, fund_df)
    log.info(f'合并资金流后：{len(enriched)} 只')

    # 4. 大盘复盘数据（含实际数据日期）
    market = fetch_market_review()
    data_date = market.get('data_date') or datetime.now().strftime('%Y%m%d')
    print(f'📅 本次复盘数据日期：{data_date}')

    # 5. 今日强势股 TOP5（收盘后重筛）
    top_picks, warnings, all_scored = screen_stocks(enriched)

    # 6. 板块温度 TOP3（基于全市场当日数据）
    sector_heat = analyze_sector_heat(enriched, top_n=3)
    if sector_heat:
        for s in sector_heat:
            print(f"  🔥 {s['theme']}: 平均{s['avg_pct']:+.1f}% | 主力{s['inflow_yi']:+.1f}亿 | 涨停{s['limit_up']}只")

    # 7. 💼 持仓结算 + 自动补仓（原持仓监控已合并于此）
    price_map = build_price_map(spot_df)
    holdings_status, closed_today, price_changes = settle_holdings(price_map, data_date)
    log.info(f'持仓结算完成：活跃 {len(holdings_status)} 只 / 今日平仓 {len(closed_today)} 只')

    # 7.1 补仓：用当日强势股 TOP 补齐空仓位（不含当日刚平仓的票，避免同日回补）
    exclude_codes = [h['code'] for h in closed_today]
    refill_candidates = top_picks
    if refill_candidates is None or refill_candidates.empty:
        refill_candidates = all_scored.head(5)
    new_positions = fill_portfolio_from_candidates(
        refill_candidates, exclude_codes=exclude_codes, source='review_refill',
    )
    if new_positions:
        log.info(f'🛒 自动补仓 {len(new_positions)} 只：'
                 f"{['%s(%s)' % (h['name'], h['code']) for h in new_positions]}")
    else:
        log.info('🛒 无需补仓（仓位已满或候选不足）')

    stats = portfolio_stats()
    if stats.get('total'):
        print(f"📈 累计战绩：{stats['total']} 笔 ｜ 胜率 {stats['win_rate']}% ｜ 平均 {stats['avg_pnl']:+.2f}%")

    # 8. 生成钉钉复盘 payload
    date_str = datetime.now().strftime('%Y-%m-%d')
    payload = generate_review_payload(
        date_str=date_str,
        top_picks=top_picks,
        warnings=warnings,
        all_stocks=all_scored.head(50),
        market=market,
        sector_heat=sector_heat,
        holdings_status=holdings_status,
        closed_today=closed_today,
        new_positions=new_positions,
        stats=stats,
        data_date=data_date,
    )

    # 9. 保存完整复盘报告
    report_path = save_review_report(
        date_str=date_str,
        top_picks=top_picks,
        warnings=warnings,
        all_stocks=all_scored.head(50),
        market=market,
        sector_heat=sector_heat,
        reports_dir=REPORTS_DIR,
        holdings_status=holdings_status,
        closed_today=closed_today,
        new_positions=new_positions,
        stats=stats,
        data_date=data_date,
    )
    log.info(f'复盘报告已保存：{report_path}')

    # 10. 推钉钉
    if TEST_ONLY:
        log.info('TEST_ONLY 模式，仅打印推送内容，不实际推送')
        print('\n--- [TEST_ONLY] 复盘推送预览 ---')
        print(f'标题: {payload["markdown"]["title"]}')
        print(payload['markdown']['text'])
        print('--- END ---\n')
    else:
        success, msg = push_to_dingtalk(DINGTALK_WEBHOOK, payload, DINGTALK_SECRET)
        if success:
            log.info(f'✅ 钉钉复盘推送成功：{msg}')
        else:
            log.error(f'❌ 钉钉复盘推送失败：{msg}')

    # 11. 控制台小结
    print('\n' + '=' * 60)
    print(f'💼 持仓（上限 {MAX_HOLDINGS} 只）｜ 止损 {DEFAULT_STOP_LOSS_PCT}% / 止盈 +{DEFAULT_TAKE_PROFIT_PCT}%')
    print('=' * 60)
    for i, r in enumerate(holdings_status, 1):
        flag = '⏳待开盘价' if r.get('entry_pending') else r.get('status_text', '')
        print(f"  {i}. {r['name']}({r['code']}) {r['buy_price']:.2f} → {r['current_price']:.2f} "
              f"{r['pnl_pct']:+.2f}%  {flag}")
    if closed_today:
        print(f'\n🚨 今日平仓 {len(closed_today)} 只：')
        for h in closed_today:
            print(f"  [{h.get('close_reason')}] {h['name']}({h['code']}) "
                  f"{h.get('closed_pnl'):+.2f}% @ {h.get('closed_price')}")
    if new_positions:
        print(f'\n🛒 今日补仓 {len(new_positions)} 只：')
        for h in new_positions:
            print(f"  ➕ {h['name']}({h['code']}) 参考价 {h['buy_price']:.2f}")

    print('\n✅ 主升浪盘后复盘运行完成')


if __name__ == '__main__':
    main()
