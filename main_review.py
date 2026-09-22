#!/usr/bin/env python3
"""
主升浪盘后复盘 - 主入口（钉钉群推送版）
=====================================
执行流程（每交易日 18:30 推送）：
1. 抓取全市场行情 + 资金流（数据未发布/读取失败时自动回退前一交易日）
2. 抓取大盘复盘数据（指数/主力/趋势/成交额/涨停跌停）
3. 💼 模拟盘持仓结算（原「持仓止盈止损监控」已合并到此）：
   - 待入场持仓按「建仓日开盘价」归一化买入成本
     （日期判断：仅当行情日 > 信号日才归一化，信号当日/重复运行一律跳过）
   - 推进交易日钟：days_held +1（仅新行情日）/ 更新期间最高价 peak_high
   - 止损：收盘盈亏 ≤ -7%
   - 移动止盈：盈利首次 ≥ +15% 启动，跟踪启动以来最高价，回撤 8% 即卖出
     （峰值用盘中最高价更新，触发用盘中最低价判定）
   - 时间止损：清理僵尸股（≥8 交易日且期间最高涨幅 <3%）与
     低效股（≥15 交易日、未启动移动止盈且期间最高涨幅 <5%）
   - 大盘弱势熔断：上证指数当日跌幅 ≤ -2% → 当天**暂停时间止损**（止损/移动止盈照常执行）
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

from src.config import TEST_ONLY, REPORTS_DIR, validate_config
from src.data_fetcher import (
    fetch_market_spot, fetch_fund_flow_rank,
    filter_main_board, enrich_with_fund_flow,
    fetch_market_review, merge_ma_panel,
)
from src.selector import screen_stocks, analyze_sector_heat, analyze_sector_leaders
from src.report import generate_review_payload, save_review_report
from src.notifier import push_all, summarize
from src.portfolio import (
    MAX_HOLDINGS, DEFAULT_STOP_LOSS_PCT,
    TRAIL_ACTIVATE_PCT, TRAIL_DRAWDOWN_PCT,
    TIME_STOP_ENABLED, TIME_STOP_REASONS,
    ZOMBIE_DAYS, ZOMBIE_PEAK_PCT, INEFFICIENT_DAYS, INEFFICIENT_PEAK_PCT,
    MARKET_FUSE_ENABLED, MARKET_FUSE_DROP_PCT, MARKET_FUSE_INDEX,
    market_fuse_check, mark_fused,
    get_active_holdings, evaluate_holding, close_holding, mark_alerted,
    normalize_pending_entries, advance_position_clock,
    fill_portfolio_from_candidates,
    update_trail_states, make_status_placeholder, portfolio_stats,
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger('market-review')


def _num(value) -> float:
    """安全转 float（NaN/None → 0.0）"""
    try:
        if value is None or pd.isna(value):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def build_price_map(spot_df: pd.DataFrame) -> dict:
    """从全市场行情构建 {code: {'open': 开盘价, 'price': 收盘价, 'high': 最高, 'low': 最低}}"""
    price_map = {}
    if spot_df is None or spot_df.empty:
        return price_map
    for _, row in spot_df.iterrows():
        code = str(row['code']).zfill(6)
        price_map[code] = {
            'open': _num(row.get('open')),
            'price': _num(row.get('price')),
            'high': _num(row.get('high')),
            'low': _num(row.get('low')),
        }
    return price_map


def settle_holdings(price_map: dict, data_date: str, index_pct_change=None):
    """
    持仓结算（合并自原持仓监控）：
    1. 待入场持仓按「建仓日开盘价」归一化买入成本（行情日必须 > 信号日）；
    2. 推进交易日钟 days_held / 期间最高价 peak_high（时间止损判定依据，幂等）；
    3. 止损：收盘盈亏 ≤ -7%；移动止盈：盈利 ≥ +15% 启动，峰值回撤 8% 卖出；
       时间止损：僵尸股（≥8 交易日且期间最高涨幅 <3%）/ 低效股（≥15 交易日且 <5%）；
       ⚡ 大盘弱势熔断：index_pct_change ≤ -2% 时**当天不执行时间止损**（保留观察，
          止损 / 移动止盈不受影响），次日大盘企稳自动补执行；
    4. 「接近启动线」标记已提醒，避免重复提醒。

    返回 (holdings_status, closed_today, price_changes, trail_started)
    """
    # 1) 归一化待入场价（信号日收盘价 → 实际建仓日开盘价）
    price_changes = normalize_pending_entries(price_map, data_date)

    # 1.5) 推进交易日钟 + 期间最高价（仅新行情日 +1，同日重复运行不改动）
    #      ⚠️ 熔断只「暂缓执行清理」，不影响考察期计时（次日企稳后条件仍满足即补执行）
    advanced = advance_position_clock(price_map, data_date)
    if advanced:
        log.info(f'⏱️ 已推进 {len(advanced)} 只持仓的交易日钟')

    # 大盘弱势熔断判定（仅拦时间止损；止损/移动止盈照常）
    fuse, fuse_msg = market_fuse_check(index_pct_change)
    if fuse:
        log.info(f'⚡ 大盘弱势熔断生效：{fuse_msg}')

    holdings = get_active_holdings()
    holdings_status, closed_today, trail_started = [], [], []
    trail_updates = []
    if not holdings:
        return holdings_status, closed_today, price_changes, trail_started

    print(f'💼 持仓结算：共 {len(holdings)} 只活跃持仓')
    for h in holdings:
        info = price_map.get(h['code'])
        cur = info['price'] if info else 0.0

        # 尚无有效行情 → 占位展示
        if not info or cur <= 0:
            holdings_status.append(make_status_placeholder(h, 0.0, 'no_quote'))
            continue

        # 待归一化的新仓：不计盈亏（实际建仓在次日开盘）
        if h.get('entry_pending'):
            holdings_status.append(make_status_placeholder(h, cur, 'pending'))
            continue

        ev = evaluate_holding(h, cur, day_high=info.get('high'), day_low=info.get('low'))
        if ev is None:
            continue

        # 移动止盈状态落盘（新启动 / 峰值抬升）
        trail_updates.append({'code': h['code'],
                              'trail_active': ev['trail_active'],
                              'trail_high': ev['trail_high']})
        if ev['trail_started']:
            trail_started.append(ev)
            print(f"  🔒 {h['name']}({h['code']}) 启动移动止盈 "
                  f"（峰值 {ev['trail_high']:.2f} / {ev['trail_peak_pnl']:+.2f}%，"
                  f"回撤线 {ev['trail_trigger_price']:.2f}）")

        # ⚡ 大盘弱势熔断：时间止损当天不执行（保留观察，避免暴跌日卖在最低点）
        if fuse and ev['trigger'] in TIME_STOP_REASONS:
            mark_fused(ev, fuse_msg)
            holdings_status.append(ev)
            peak_txt = (f"{ev['peak_high_pnl']:+.2f}%"
                        if ev.get('peak_high_pnl') is not None else '-')
            print(f"  ⚡ {h['name']}({h['code']}) 时间止损暂缓（大盘熔断）"
                  f"：持有 {ev.get('days_held')} 交易日 / 期间最高 {peak_txt} → 保留观察")
            continue

        if ev['trigger'] in ('stop_loss', 'take_profit') or ev['trigger'] in TIME_STOP_REASONS:
            reason = ev['trigger']
            # 移动止盈用 trail 峰值；其余（止损/时间止损）用期间最高涨幅
            peak = ev['trail_peak_pnl'] if reason == 'take_profit' else ev['peak_high_pnl']
            closed = close_holding(h['code'], ev['exit_pnl_pct'], reason=reason,
                                   price=ev['exit_price'], peak_pnl=peak,
                                   days_held=ev.get('days_held'))
            if closed:
                closed_today.append(closed)
                if reason == 'take_profit':
                    print(f"  🔒 {h['name']}({h['code']}) 移动止盈卖出 "
                          f"{ev['exit_pnl_pct']:+.2f}%（峰值 {ev['trail_peak_pnl']:+.2f}% "
                          f"→ 回撤至 {ev['exit_price']:.2f}）")
                elif reason == 'stop_loss':
                    print(f"  🚨 {h['name']}({h['code']}) 触发止损 "
                          f"{ev['exit_pnl_pct']:+.2f}% → 已平仓")
                else:
                    icon = '🧟' if reason == 'time_stop_zombie' else '🐌'
                    label = '僵尸股' if reason == 'time_stop_zombie' else '低效股'
                    peak_txt = (f"{ev['peak_high_pnl']:+.2f}%"
                                if ev.get('peak_high_pnl') is not None else '-')
                    print(f"  {icon} {h['name']}({h['code']}) 时间止损·{label}清理 "
                          f"{ev['exit_pnl_pct']:+.2f}%（持有 {ev.get('days_held')} 交易日 / "
                          f"期间最高 {peak_txt}）→ 已平仓（让位补仓）")
            continue  # 已平仓，不再计入活跃持仓表

        holdings_status.append(ev)
        if ev['trigger'] == 'warning':
            mark_alerted(h['code'])
            print(f"  ⚡ {h['name']}({h['code']}) 接近启动线 {ev['pnl_pct']:+.2f}%（已提醒）")

    # 批量落盘移动止盈状态（一次写盘）
    n = update_trail_states(trail_updates)
    if n:
        print(f'  💾 移动止盈状态已更新 {n} 只')

    return holdings_status, closed_today, price_changes, trail_started


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

    # 3.5 合并均线面板（趋势线 MA120）—— 取不到数据时原样返回，技术面自动退回原口径
    enriched = merge_ma_panel(enriched)

    # 4. 大盘复盘数据（含实际数据日期）
    market = fetch_market_review()
    data_date = market.get('data_date') or datetime.now().strftime('%Y%m%d')
    print(f'📅 本次复盘数据日期：{data_date}')
    idx_pct = market.get('index_pct_change')
    if MARKET_FUSE_ENABLED and idx_pct is not None:
        fuse_now, fuse_now_msg = market_fuse_check(idx_pct)
        print(f'{MARKET_FUSE_INDEX} 当日 {idx_pct:+.2f}%'
              + (f'  ⚡ 触发大盘熔断（阈值 {MARKET_FUSE_DROP_PCT}%）' if fuse_now else ''))

    # 5. 今日强势股 TOP5（收盘后重筛）
    top_picks, warnings, all_scored = screen_stocks(enriched)

    # 6. 板块方向（【任务②】三榜：涨幅 / 主力资金 / 涨停家数，独立排序）
    #    原「板块温度」= 涨幅×0.4+资金×0.3+涨停×0.3，三项量纲混杂、实际由资金主导
    #    （实测资金项话语权是涨幅的 24.5 倍）→ 拆成三榜，口径透明。
    sector_leaders = analyze_sector_leaders(enriched, top_n=3)
    _cov = sector_leaders.get('coverage') or {}
    if _cov:
        log.info('板块归类覆盖：%s/%s 只（%s%%）｜ %s 个板块',
                 _cov.get('tagged'), _cov.get('total'), _cov.get('ratio'), _cov.get('n_sector'))
    for _key, _title in (('gainers', '涨幅'), ('flows', '资金'), ('limit_ups', '涨停')):
        for _i, _s in enumerate(sector_leaders.get(_key) or [], 1):
            print(f"  🔥 {_title}榜#{_i} {_s['theme']}: 平均{_s['avg_pct']:+.1f}% | "
                  f"主力{_s['inflow_yi']:+.1f}亿 | 涨停{_s['limit_up']}只 | {_s['cnt']}只成分")
    # 三榜为空时回退旧混合榜（报告侧仍支持，回滚零成本）
    sector_heat = None if sector_leaders.get('all') else analyze_sector_heat(enriched, top_n=3)

    # 7. 💼 持仓结算 + 自动补仓（原持仓监控已合并于此）
    price_map = build_price_map(spot_df)
    holdings_status, closed_today, price_changes, trail_started = settle_holdings(
        price_map, data_date, index_pct_change=idx_pct)
    log.info(f'持仓结算完成：活跃 {len(holdings_status)} 只 / 今日平仓 {len(closed_today)} 只 / '
             f'新启动移动止盈 {len(trail_started)} 只')

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
        sector_leaders=sector_leaders,
        holdings_status=holdings_status,
        closed_today=closed_today,
        new_positions=new_positions,
        trail_started=trail_started,
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
        sector_leaders=sector_leaders,
        reports_dir=REPORTS_DIR,
        holdings_status=holdings_status,
        closed_today=closed_today,
        new_positions=new_positions,
        trail_started=trail_started,
        stats=stats,
        data_date=data_date,
    )
    log.info(f'复盘报告已保存：{report_path}')

    # 10. 推送（钉钉 + 飞书 双通道，未配置的通道自动跳过）
    if TEST_ONLY:
        log.info('TEST_ONLY 模式，仅打印推送内容，不实际推送')
        print('\n--- [TEST_ONLY] 复盘推送预览 ---')
        print(f'标题: {payload["markdown"]["title"]}')
        print(payload['markdown']['text'])
        print('--- END ---\n')
    else:
        results = push_all(payload, log=log)
        log.info(f'📤 推送结果：{summarize(results)}')
        if not any(ok for ok, _ in results.values()):
            log.error('❌ 所有通道推送均失败，请检查 webhook 配置')

    # 11. 控制台小结
    print('\n' + '=' * 60)
    print(f'💼 持仓（上限 {MAX_HOLDINGS} 只）｜ 止损 {DEFAULT_STOP_LOSS_PCT}% ｜ '
          f'移动止盈：+{TRAIL_ACTIVATE_PCT}% 启动 / 回撤 {TRAIL_DRAWDOWN_PCT}% 卖出')
    if TIME_STOP_ENABLED:
        print(f'🧹 时间止损：僵尸 ≥{ZOMBIE_DAYS}日且期间最高 <{ZOMBIE_PEAK_PCT}% ｜ '
              f'低效 ≥{INEFFICIENT_DAYS}日且未启动且 <{INEFFICIENT_PEAK_PCT}%')
    if MARKET_FUSE_ENABLED:
        print(f'⚡ 大盘熔断：{MARKET_FUSE_INDEX} 单日 ≤{MARKET_FUSE_DROP_PCT}% '
              f'→ 当天暂停时间止损（止损/移动止盈不受影响）')
    print('=' * 60)
    for i, r in enumerate(holdings_status, 1):
        flag = '⏳待开盘价' if r.get('entry_pending') else r.get('status_text', '')
        peak = (f" 峰值{r['trail_high']:.2f}({r['trail_peak_pnl']:+.1f}%)/回撤线{r['trail_trigger_price']:.2f}"
                if r.get('trail_active') and r.get('trail_high') else '')
        clock = ''
        if not r.get('entry_pending'):
            tk = f" {r['time_stop_hint']}" if r.get('time_stop_hint') else ''
            clock = f"  持有{int(r.get('days_held') or 0)}日{tk}"
        print(f"  {i}. {r['name']}({r['code']}) {r['buy_price']:.2f} → {r['current_price']:.2f} "
              f"{r['pnl_pct']:+.2f}%  {flag}{peak}{clock}")
    fused_rows = [r for r in holdings_status if r.get('fused')]
    if fused_rows:
        print(f'\n⚡ 大盘熔断：今日暂缓时间止损 {len(fused_rows)} 只'
              f'（持仓不变，大盘企稳后自动补执行）：')
        for r in fused_rows:
            peak = r.get('peak_high_pnl')
            peak_txt = f'{peak:+.2f}%' if peak is not None else '-'
            print(f"  [{r.get('status_text')}] {r['name']}({r['code']}) "
                  f"持有 {int(r.get('days_held') or 0)} 日 / 期间最高 {peak_txt}")
    if trail_started:
        print(f'\n🔒 今日启动移动止盈 {len(trail_started)} 只：')
        for r in trail_started:
            print(f"  🔒 {r['name']}({r['code']}) 峰值 {r['trail_high']:.2f}"
                  f"（{r['trail_peak_pnl']:+.2f}%）→ 回撤线 {r['trail_trigger_price']:.2f}")
    if closed_today:
        print(f'\n🚨 今日平仓 {len(closed_today)} 只：')
        for h in closed_today:
            reason = h.get('close_reason')
            if reason == 'take_profit':
                label = '移动止盈'
            elif reason == 'stop_loss':
                label = '止损'
            elif reason == 'time_stop_zombie':
                label = '🧟 时间止损·僵尸'
            elif reason == 'time_stop_inefficient':
                label = '🐌 时间止损·低效'
            else:
                label = reason
            peak = h.get('closed_peak_pnl')
            peak_txt = f"（峰值{peak:+.2f}%）" if peak is not None else ''
            days = h.get('closed_days_held')
            days_txt = f" 持有{days}日" if days is not None else ''
            print(f"  [{label}] {h['name']}({h['code']}) "
                  f"{h.get('closed_pnl'):+.2f}%{peak_txt}{days_txt} @ {h.get('closed_price')}")
    if new_positions:
        print(f'\n🛒 今日补仓 {len(new_positions)} 只：')
        for h in new_positions:
            print(f"  ➕ {h['name']}({h['code']}) 参考价 {h['buy_price']:.2f}")

    print('\n✅ 主升浪盘后复盘运行完成')


if __name__ == '__main__':
    main()
