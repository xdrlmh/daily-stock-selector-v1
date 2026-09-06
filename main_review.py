#!/usr/bin/env python3
"""
主升浪盘后复盘 - 主入口（钉钉群推送版）
=====================================
执行流程（15:30 推送）：
1. 抓取全市场行情 + 资金流
2. 抓取大盘复盘数据（指数/主力/趋势/成交额/涨停跌停）
3. 今日强势股 TOP5（五维评分，收盘后数据）
4. 板块温度 TOP3（按题材聚合力强板块）
5. 生成钉钉复盘 payload + 保存完整复盘报告
6. 推钉钉群

与早盘 main.py 互补：早盘看「今天关注什么」，盘后看「今天发生了什么、明天怎么看」。
"""
import sys
import logging
from datetime import datetime
from pathlib import Path

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

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger('market-review')


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

    # 2. 抓取数据（早盘同源）
    spot_df = fetch_market_spot()
    if spot_df.empty:
        log.error('行情数据抓取失败，退出')
        sys.exit(1)

    fund_df = fetch_fund_flow_rank()

    # 3. 过滤主板非ST + 合并资金流
    main_board = filter_main_board(spot_df)
    log.info(f'主板非ST候选：{len(main_board)} 只')
    enriched = enrich_with_fund_flow(main_board, fund_df)
    log.info(f'合并资金流后：{len(enriched)} 只')

    # 4. 大盘复盘数据
    market = fetch_market_review()

    # 5. 今日强势股 TOP5（收盘后重筛）
    top_picks, warnings, all_scored = screen_stocks(enriched)

    # 6. 板块温度 TOP3（基于全市场当日数据）
    sector_heat = analyze_sector_heat(enriched, top_n=3)
    if sector_heat:
        for s in sector_heat:
            print(f"  🔥 {s['theme']}: 平均{s['avg_pct']:+.1f}% | 主力{s['inflow_yi']:+.1f}亿 | 涨停{s['limit_up']}只")

    # 7. 生成钉钉复盘 payload
    date_str = datetime.now().strftime('%Y-%m-%d')
    payload = generate_review_payload(
        date_str=date_str,
        top_picks=top_picks,
        warnings=warnings,
        all_stocks=all_scored.head(50),
        market=market,
        sector_heat=sector_heat,
    )

    # 8. 保存完整复盘报告
    report_path = save_review_report(
        date_str=date_str,
        top_picks=top_picks,
        warnings=warnings,
        all_stocks=all_scored.head(50),
        market=market,
        sector_heat=sector_heat,
        reports_dir=REPORTS_DIR,
    )
    log.info(f'复盘报告已保存：{report_path}')

    # 9. 推钉钉
    if TEST_ONLY:
        log.info('TEST_ONLY 模式，仅打印推送内容，不实际推送')
        print('\n--- [TEST_ONLY] 复盘推送预览 ---')
        print(f'标题: {payload["markdown"]["title"]}')
        print(payload['markdown']['text'][:600] + '...')
        print('--- END ---\n')
    else:
        success, msg = push_to_dingtalk(DINGTALK_WEBHOOK, payload, DINGTALK_SECRET)
        if success:
            log.info(f'✅ 钉钉复盘推送成功：{msg}')
        else:
            log.error(f'❌ 钉钉复盘推送失败：{msg}')

    print('\n✅ 主升浪盘后复盘运行完成')


if __name__ == '__main__':
    main()
