"""
数据获取模块 - 基于 akshare（免费开源 A 股数据）
"""
import akshare as ak
import pandas as pd
import time
from typing import List, Dict, Any
import warnings

warnings.filterwarnings('ignore')


def fetch_market_spot() -> pd.DataFrame:
    """
    获取全市场实时行情
    返回字段：代码、名称、最新价、涨跌幅、5日涨幅、换手率、市盈率-动、
             总市值、流通市值、量比
    """
    print('📡 抓取全市场实时行情...')
    try:
        df = ak.stock_zh_a_spot_em()
        print(f'  ✅ 共 {len(df)} 条记录')
        # 列名映射（akshare 列名为中文）
        column_map = {
            '代码': 'code',
            '名称': 'name',
            '最新价': 'price',
            '涨跌幅': 'pct_change',
            '涨跌额': 'change',
            '成交量': 'volume',
            '成交额': 'turnover',
            '振幅': 'amplitude',
            '最高': 'high',
            '最低': 'low',
            '今开': 'open',
            '昨收': 'prev_close',
            '换手率': 'turnover_rate',
            '市盈率-动态': 'pe_ttm',
            '市净率': 'pb',
            '总市值': 'total_mcap',
            '流通市值': 'circ_mcap',
            '量比': 'volume_ratio',
            '60日涨跌幅': 'pct_60d',
            '年初至今涨跌幅': 'pct_ytd',
        }
        df = df.rename(columns=column_map)
        # 仅保留我们关心的列
        keep_cols = [c for c in column_map.values() if c in df.columns]
        df = df[keep_cols].copy()
        return df
    except Exception as e:
        print(f'  ❌ 抓取行情失败: {e}')
        return pd.DataFrame()


def fetch_fund_flow_rank() -> pd.DataFrame:
    """
    获取个股资金流排行（主力净流入）
    """
    print('📡 抓取个股资金流排行...')
    try:
        # 主力净流入排行
        df = ak.stock_individual_fund_flow_rank(indicator='今日')
        # 列名映射
        column_map = {
            '代码': 'code',
            '名称': 'name',
            '最新价': 'price',
            '今日涨跌幅': 'pct_change',
            '主力净流入-净额': 'main_net_inflow',
            '主力净流入-净占比': 'main_net_inflow_pct',
            '超大单净流入-净额': 'super_large_inflow',
            '大单净流入-净额': 'large_inflow',
            '中单净流入-净额': 'medium_inflow',
            '小单净流入-净额': 'small_inflow',
        }
        df = df.rename(columns=column_map)
        keep_cols = [c for c in column_map.values() if c in df.columns]
        df = df[keep_cols].copy()
        print(f'  ✅ 共 {len(df)} 条资金流记录')
        return df
    except Exception as e:
        print(f'  ❌ 抓取资金流失败: {e}')
        return pd.DataFrame()


def filter_main_board(df: pd.DataFrame) -> pd.DataFrame:
    """
    过滤：沪深主板 + 非ST + 排除停牌
    主板规则：
    - 60xxxx.SH：沪市主板
    - 000xxx.SZ / 001xxx.SZ / 002xxx.SZ：深市主板（注意：002 开头是中小板，
      但 2021 年合并后归入深市主板）
    排除：
    - 30xxxx：创业板
    - 688xxx：科创板
    - 8xxxxxx / 4xxxxxx：北交所
    - ST / *ST（名称含）
    """
    if df.empty:
        return df

    df = df.copy()
    df['code_str'] = df['code'].astype(str).str.zfill(6)

    def is_main_board(code: str) -> bool:
        # 沪市主板 60xxxx
        if code.startswith('60'):
            return True
        # 深市主板 000xxx, 001xxx, 002xxx
        if code.startswith(('000', '001', '002')):
            return True
        return False

    df = df[df['code_str'].apply(is_main_board)]

    # 排除 ST / *ST
    df = df[~df['name'].str.contains('ST', case=False, na=False)]

    # 排除停牌（最新价为 0 或 NaN）
    df = df[(df['price'].notna()) & (df['price'] > 0)]

    # 排除市值过小（流动性差）
    df = df[df['circ_mcap'].notna() & (df['circ_mcap'] >= 30e8)]

    return df


def enrich_with_fund_flow(spot_df: pd.DataFrame, fund_df: pd.DataFrame) -> pd.DataFrame:
    """
    用资金流数据丰富行情数据
    """
    if spot_df.empty or fund_df.empty:
        if 'main_net_inflow' not in spot_df.columns:
            spot_df['main_net_inflow'] = 0.0
        return spot_df

    # 仅保留代码列做关联
    fund_subset = fund_df[['code', 'main_net_inflow', 'main_net_inflow_pct']].copy()
    fund_subset['code'] = fund_subset['code'].astype(str).str.zfill(6)

    spot_df = spot_df.copy()
    spot_df['code_str'] = spot_df['code'].astype(str).str.zfill(6)
    spot_df = spot_df.merge(
        fund_subset,
        left_on='code_str',
        right_on='code',
        how='left',
        suffixes=('', '_fund')
    )
    spot_df['main_net_inflow'] = spot_df['main_net_inflow'].fillna(0.0)
    spot_df['main_net_inflow_pct'] = spot_df['main_net_inflow_pct'].fillna(0.0)
    return spot_df


def fetch_history_kline(code: str, days: int = 60) -> pd.DataFrame:
    """
    获取单只股票的 K 线数据（用于计算均线/MACD）
    """
    try:
        code_with_prefix = code
        if not code.endswith(('.SH', '.SZ')):
            if code.startswith('6'):
                code_with_prefix = f'{code}.SH'
            else:
                code_with_prefix = f'{code}.SZ'

        end_date = pd.Timestamp.now().strftime('%Y%m%d')
        start_date = (pd.Timestamp.now() - pd.Timedelta(days=days * 2)).strftime('%Y%m%d')

        df = ak.stock_zh_a_hist(
            symbol=code[:6],
            period='daily',
            start_date=start_date,
            end_date=end_date,
            adjust='qfq'
        )
        return df
    except Exception as e:
        return pd.DataFrame()


if __name__ == '__main__':
    # 简单测试
    spot = fetch_market_spot()
    print(f'\n全市场行情: {len(spot)} 条')

    main_board = filter_main_board(spot)
    print(f'主板非ST: {len(main_board)} 条')

    fund = fetch_fund_flow_rank()
    print(f'资金流排行: {len(fund)} 条')

    enriched = enrich_with_fund_flow(main_board, fund)
    print(f'合并后: {len(enriched)} 条')
    print(enriched.head(3))