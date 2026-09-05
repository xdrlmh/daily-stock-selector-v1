"""
数据获取模块 - 基于 Tushare（正规金融数据 API）
=================================================
替代 akshare，避免 GitHub Actions 云端 IP 被东方财富断连/限流。

Tushare API 文档: https://tushare.pro/document/2
需要环境变量: TUSHARE_TOKEN（在 GitHub Secrets 中配置）
"""
import os
import tushare as ts
import pandas as pd
from datetime import datetime, timedelta
from typing import List, Dict, Any
import warnings

warnings.filterwarnings('ignore')


# ============ 初始化 Tushare ============
def _init_tushare() -> Any:
    """从环境变量读取 token，初始化 pro API"""
    token = os.environ.get('TUSHARE_TOKEN', '').strip()
    if not token:
        raise ValueError(
            '未配置 TUSHARE_TOKEN 环境变量。\n'
            '请在 GitHub 仓库 Settings → Secrets 中添加 TUSHARE_TOKEN。\n'
            '获取方式：https://tushare.pro/user/token'
        )
    ts.set_token(token)
    return ts.pro_api()


# ============ 工具函数 ============
def _get_trade_dates(pro: Any, days_back_list: List[int]) -> Dict[int, str]:
    """获取 N 个交易日前的日期，返回 {N: 'YYYYMMDD'}，N=0 为最近交易日"""
    max_days = max(days_back_list) * 2 + 30
    cal = pro.trade_cal(
        exchange='SSE', is_open='1',
        start_date=(datetime.now() - timedelta(days=max_days)).strftime('%Y%m%d'),
        end_date=datetime.now().strftime('%Y%m%d'),
    )
    if cal is None or cal.empty:
        raise RuntimeError('未找到交易日历')
    cal_sorted = cal.sort_values('cal_date', ascending=False).reset_index(drop=True)
    result = {}
    for n in days_back_list:
        if n < len(cal_sorted):
            result[n] = cal_sorted.iloc[n]['cal_date']
    return result


def _get_stock_names(pro: Any) -> pd.DataFrame:
    """获取全部上市股票代码-名称映射"""
    basic = pro.stock_basic(
        exchange='', list_status='L',
        fields='ts_code,symbol,name,market'
    )
    return basic


# ============ 行情抓取 ============
def fetch_market_spot() -> pd.DataFrame:
    """
    获取全市场行情（基于最近交易日 EOD 数据）
    字段：code, name, price, pct_change, pct_5d, pct_60d,
          turnover_rate, pe_ttm, pb, total_mcap, circ_mcap,
          volume_ratio, volume, turnover, high, low, open, pre_close, change
    """
    print('📡 抓取全市场行情（Tushare）...')
    try:
        pro = _init_tushare()

        dates = _get_trade_dates(pro, [0, 5, 60])
        today = dates[0]
        d_5d = dates.get(5, today)
        d_60d = dates.get(60, today)
        print(f'  📅 最近交易日={today} | 5日前={d_5d} | 60日前={d_60d}')

        # 1. 最近交易日日线行情
        daily_today = pro.daily(trade_date=today)
        if daily_today is None or daily_today.empty:
            print(f'  ⚠️ 当日无行情（{today}, 可能非交易日）')
            return pd.DataFrame()
        print(f'  ✅ 当日行情: {len(daily_today)} 条')

        # 2. 当日估值基础
        basic_today = pro.daily_basic(
            trade_date=today,
            fields='ts_code,trade_date,pe,pe_ttm,pb,total_mv,circ_mv,turnover_rate,volume_ratio'
        )
        print(f'  ✅ 当日估值: {len(basic_today)} 条')

        # 3. 股票名称映射
        stock_names = _get_stock_names(pro)
        name_map = stock_names.set_index('ts_code')['name'].to_dict()
        print(f'  ✅ 股票名称: {len(name_map)} 条')

        # 4. 5d / 60d 收盘价（用于算趋势涨幅）
        daily_5d = pro.daily(trade_date=d_5d)[['ts_code', 'close']].rename(
            columns={'close': 'close_5d'})
        daily_60d = pro.daily(trade_date=d_60d)[['ts_code', 'close']].rename(
            columns={'close': 'close_60d'})

        # 5. 合并 + 算涨幅
        df = daily_today.merge(basic_today, on=['ts_code', 'trade_date'], how='left')
        df = df.merge(daily_5d, on='ts_code', how='left')
        df = df.merge(daily_60d, on='ts_code', how='left')
        df['name'] = df['ts_code'].map(name_map)
        df['pct_5d'] = ((df['close'] - df['close_5d']) / df['close_5d'] * 100).round(2)
        df['pct_60d'] = ((df['close'] - df['close_60d']) / df['close_60d'] * 100).round(2)

        # 6. 字段映射
        df['code'] = df['ts_code'].str.split('.').str[0]
        df = df.rename(columns={
            'close': 'price',
            'pct_chg': 'pct_change',
            'vol': 'volume',
            'amount': 'turnover',
        })
        # Tushare total_mv / circ_mv 单位万元 → 元
        df['total_mcap'] = df['total_mv'] * 1e4
        df['circ_mcap'] = df['circ_mv'] * 1e4

        keep_cols = [
            'code', 'name', 'price', 'pct_change',
            'pct_5d', 'pct_60d',
            'volume', 'turnover',
            'open', 'high', 'low', 'pre_close', 'change',
            'turnover_rate', 'pe_ttm', 'pb',
            'total_mcap', 'circ_mcap', 'volume_ratio',
        ]
        keep_cols = [c for c in keep_cols if c in df.columns]
        df = df[keep_cols].copy()

        print(f'  ✅ 共 {len(df)} 条记录')
        return df
    except Exception as e:
        print(f'  ❌ 抓取行情失败: {e}')
        import traceback
        traceback.print_exc()
        return pd.DataFrame()


def fetch_fund_flow_rank() -> pd.DataFrame:
    """
    获取个股资金流（主力净流入）
    返回：code, name, main_net_inflow, main_net_inflow_pct
    """
    print('📡 抓取个股资金流（Tushare）...')
    try:
        pro = _init_tushare()
        trade_date = _get_trade_dates(pro, [0])[0]

        df = pro.moneyflow(trade_date=trade_date)
        if df is None or df.empty:
            print(f'  ⚠️ 资金流数据为空（{trade_date}）')
            return pd.DataFrame()

        # net_mf_amount 单位万元 → 元
        df['main_net_inflow'] = df['net_mf_amount'] * 1e4
        df['main_net_inflow_pct'] = 0.0  # moneyflow 表无 amount 字段，简化为 0
        df['code'] = df['ts_code'].str.split('.').str[0]

        keep_cols = ['code', 'main_net_inflow', 'main_net_inflow_pct',
                     'buy_sm_amount', 'sell_sm_amount',
                     'buy_md_amount', 'sell_md_amount',
                     'buy_lg_amount', 'sell_lg_amount',
                     'buy_elg_amount', 'sell_elg_amount']
        keep_cols = [c for c in keep_cols if c in df.columns]
        df = df[keep_cols].copy()
        print(f'  ✅ 共 {len(df)} 条资金流记录')
        return df
    except Exception as e:
        print(f'  ❌ 抓取资金流失败: {e}')
        import traceback
        traceback.print_exc()
        return pd.DataFrame()


def filter_main_board(df: pd.DataFrame) -> pd.DataFrame:
    """
    过滤：沪深主板 + 非ST + 排除停牌 + 排除市值过小
    主板规则：
    - 60xxxx.SH：沪市主板
    - 000xxx / 001xxx / 002xxx：深市主板（002 原中小板，2021 后并入主板）
    排除 30xxxx（创业板）、688xxx（科创板）、8xxxxxx/4xxxxxx（北交所）
    """
    if df is None or df.empty:
        return df

    df = df.copy()
    df['code_str'] = df['code'].astype(str).str.zfill(6)

    def is_main_board(code: str) -> bool:
        if code.startswith('60'):
            return True
        if code.startswith(('000', '001', '002')):
            return True
        return False

    df = df[df['code_str'].apply(is_main_board)]
    df = df[~df['name'].astype(str).str.contains('ST', case=False, na=False)]
    df = df[(df['price'].notna()) & (df['price'] > 0)]
    df = df[df['circ_mcap'].notna() & (df['circ_mcap'] >= 30e8)]
    return df


def enrich_with_fund_flow(spot_df: pd.DataFrame, fund_df: pd.DataFrame) -> pd.DataFrame:
    """用资金流数据丰富行情数据"""
    if spot_df is None or spot_df.empty or fund_df is None or fund_df.empty:
        if spot_df is not None and 'main_net_inflow' not in spot_df.columns:
            spot_df['main_net_inflow'] = 0.0
        if spot_df is not None and 'main_net_inflow_pct' not in spot_df.columns:
            spot_df['main_net_inflow_pct'] = 0.0
        return spot_df

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
    """获取单只股票的 K 线数据（基于 Tushare）"""
    try:
        pro = _init_tushare()
        if not code.endswith(('.SH', '.SZ')):
            ts_code = f'{code}.SZ' if not code.startswith('6') else f'{code}.SH'
        else:
            ts_code = code

        end_date = datetime.now().strftime('%Y%m%d')
        start_date = (datetime.now() - timedelta(days=days * 2)).strftime('%Y%m%d')
        df = pro.daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
        return df
    except Exception as e:
        return pd.DataFrame()


def fetch_market_overview() -> Dict[str, Any]:
    """
    获取大盘环境（基于上证指数 000001.SH）：
    - index_close: 收盘点位
    - index_pct_change: 当日涨跌幅（%）
    - main_net_inflow_yi: 大盘主力净额（亿元，moneyflow 全市场 sum）
    - trend_60d: 60日趋势分类
      取值：'up'（上涨）/'down'（下跌）/'sideways'（震荡）/'unknown'（数据不足）
    - trend_detail: 趋势详情字符串（MA20/MA60 等）
    - data_date: 数据日期
    """
    print('📡 抓取大盘环境（上证指数）...')
    result: Dict[str, Any] = {
        'index_close': None,
        'index_pct_change': None,
        'main_net_inflow_yi': None,
        'trend_60d': 'unknown',
        'trend_detail': '',
        'data_date': '',
    }
    try:
        pro = _init_tushare()
        trade_date = _get_trade_dates(pro, [0])[0]
        result['data_date'] = trade_date

        # 1. 上证指数当日行情
        idx_today = pro.index_daily(ts_code='000001.SH', trade_date=trade_date)
        if idx_today is not None and not idx_today.empty:
            row = idx_today.iloc[0]
            result['index_close'] = float(row['close'])
            result['index_pct_change'] = float(row['pct_chg'])
            print(f'  ✅ 上证指数: {row["close"]:.2f} ({row["pct_chg"]:+.2f}%)')
        else:
            print('  ⚠️ 上证指数当日行情为空')

        # 2. 上证指数60日 K线 → MA20/MA60 趋势判断
        start_date = (datetime.now() - timedelta(days=130)).strftime('%Y%m%d')
        idx_hist = pro.index_daily(ts_code='000001.SH', start_date=start_date, end_date=trade_date)
        if idx_hist is not None and len(idx_hist) >= 60:
            closes = idx_hist.sort_values('trade_date')['close'].astype(float).values
            closes = closes[-60:]  # 取最近60个交易日
            ma20 = float(closes[-20:].mean())
            ma60 = float(closes.mean())
            ma_diff_pct = (ma20 - ma60) / ma60 * 100
            current = float(closes[-1])

            if ma_diff_pct > 2.0 and current > ma20:
                result['trend_60d'] = 'up'
                emoji = '📈'
                label = '上涨趋势'
            elif ma_diff_pct < -2.0 and current < ma20:
                result['trend_60d'] = 'down'
                emoji = '📉'
                label = '下跌趋势'
            else:
                result['trend_60d'] = 'sideways'
                emoji = '〰️'
                label = '震荡整理'

            result['trend_detail'] = (
                f'{emoji}{label} '
                f'(MA20={ma20:.0f} vs MA60={ma60:.0f}, 偏离 {ma_diff_pct:+.1f}%)'
            )
            print(f'  ✅ 60日趋势: {result["trend_detail"]}')
        else:
            print(f'  ⚠️ 上证指数60日数据不足（仅 {0 if idx_hist is None else len(idx_hist)} 条）')

        # 3. 全市场主力净额（moneyflow 大单合计）
        mf = pro.moneyflow(trade_date=trade_date)
        if mf is not None and not mf.empty:
            # buy_lg_amount + buy_elg_amount - sell_lg_amount - sell_elg_amount = 大单净额（万元）
            # 主力 = 大单 + 特大单
            big_net_wan = (
                mf['buy_lg_amount'].fillna(0).sum()
                + mf['buy_elg_amount'].fillna(0).sum()
                - mf['sell_lg_amount'].fillna(0).sum()
                - mf['sell_elg_amount'].fillna(0).sum()
            )
            result['main_net_inflow_yi'] = big_net_wan / 1e4  # 万 → 亿
            print(f'  ✅ 大盘主力净额: {result["main_net_inflow_yi"]:+.1f} 亿')
        else:
            print('  ⚠️ 资金流数据为空')

    except Exception as e:
        print(f'  ❌ 抓取大盘环境失败: {e}')
        import traceback
        traceback.print_exc()
    return result


if __name__ == '__main__':
    spot = fetch_market_spot()
    print(f'\n全市场行情: {len(spot)} 条')

    main_board = filter_main_board(spot)
    print(f'主板非ST: {len(main_board)} 条')

    fund = fetch_fund_flow_rank()
    print(f'资金流排行: {len(fund)} 条')

    enriched = enrich_with_fund_flow(main_board, fund)
    print(f'合并后: {len(enriched)} 条')
    print(enriched.head(3))
