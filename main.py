#!/usr/bin/env python3
"""
主升浪日报 - 主入口（钉钉群推送版）
=====================================
执行流程：
1. 抓取全市场行情
2. 抓取资金流排行
3. 过滤：主板非ST
4. 五维评分
5. 输出 TOP 5 + 警示名单
6. 保存完整报告
7. 推钉钉群

推送通道：自定义 webhook 机器人（钉钉群 → 您的手机）
"""
import sys
import os
import logging
from datetime import datetime
from pathlib import Path

# 确保 src 包可导入
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.config import DINGTALK_WEBHOOK, DINGTALK_SECRET, TEST_ONLY, REPORTS_DIR, validate_config
from src.data_fetcher import (
    fetch_market_spot, fetch_fund_flow_rank,
    filter_main_board, enrich_with_fund_flow,
)
from src.selector import screen_stocks, fallback_from_top_gainers
from src.report import generate_dingtalk_payload, save_full_report
from src.dingtalk import push_to_dingtalk


# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger('main-surge')


def main():
    print('=' * 60)
    print(f'🚀 主升浪日报 启动 - {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
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

    # 2. 抓取数据
    spot_df = fetch_market_spot()
    if spot_df.empty:
        log.error('行情数据抓取失败，退出')
        sys.exit(1)

    fund_df = fetch_fund_flow_rank()

    # 3. 过滤主板非ST
    main_board = filter_main_board(spot_df)
    log.info(f'主板非ST候选：{len(main_board)} 只')

    # 4. 合并资金流数据
    enriched = enrich_with_fund_flow(main_board, fund_df)
    log.info(f'合并资金流后：{len(enriched)} 只')

    # 5. 评分 + 筛选
    top_picks, warnings, all_scored = screen_stocks(enriched)

    # 6. 兜底：若候选不足，从涨幅榜补齐
    if len(top_picks) < 3:
        log.warning(f'TOP 仅 {len(top_picks)} 只，启用兜底逻辑')
        fallback = fallback_from_top_gainers(enriched, n=5 - len(top_picks))
        if not fallback.empty:
            import pandas as pd
            top_picks = pd.concat([top_picks, fallback]).drop_duplicates('code').head(5)

    # 7. 生成报告
    date_str = datetime.now().strftime('%Y-%m-%d')

    # 7.1 钉钉消息 payload
    dingtalk_payload = generate_dingtalk_payload(
        date_str=date_str,
        top_picks=top_picks,
        warnings=warnings,
        all_stocks=all_scored.head(50),
    )

    # 7.2 完整报告（保存到 reports/）
    full_report_path = save_full_report(
        date_str=date_str,
        top_picks=top_picks,
        warnings=warnings,
        all_stocks=all_scored.head(50),
        reports_dir=REPORTS_DIR,
    )
    log.info(f'完整报告已保存：{full_report_path}')

    # 8. 推钉钉
    if TEST_ONLY:
        log.info('TEST_ONLY 模式，仅打印推送内容，不实际推送')
        print('\n--- [TEST_ONLY] 推送预览 ---')
        print(f'标题: {dingtalk_payload["markdown"]["title"]}')
        print(dingtalk_payload['markdown']['text'][:500] + '...')
        print('--- END ---\n')
    else:
        success, msg = push_to_dingtalk(DINGTALK_WEBHOOK, dingtalk_payload, DINGTALK_SECRET)
        if success:
            log.info(f'✅ 钉钉推送成功：{msg}')
        else:
            log.error(f'❌ 钉钉推送失败：{msg}')

    # 9. 控制台输出 TOP 5
    print('\n' + '=' * 60)
    print(f'📊 TOP {len(top_picks)} 精选')
    print('=' * 60)
    if not top_picks.empty:
        print(f"{'#':<3} {'代码':<10} {'名称':<12} {'现价':<8} {'当日':<8} {'主力':<10} {'评分':<6}")
        for i, (_, row) in enumerate(top_picks.iterrows(), 1):
            print(
                f"{i:<3} {row['code']:<10} {row['name']:<12} "
                f"{row['price']:<8.2f} {row['pct_change']:+.2f}%   "
                f"{row.get('main_net_inflow', 0)/1e8:+.1f}亿   "
                f"{row['total_score']:<6.0f}"
            )

    print('\n' + '=' * 60)
    print(f'⚠️ 警示名单（{len(warnings)} 只）')
    print('=' * 60)
    if not warnings.empty:
        for _, row in warnings.iterrows():
            pct = row.get('pct_5d', row.get('pct_change', 0))
            print(f"  {row['code']} {row['name']} - {pct:+.2f}%")

    print('\n✅ 主升浪日报运行完成')


if __name__ == '__main__':
    main()