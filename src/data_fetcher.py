"""
数据获取模块 - 基于 Tushare（正规金融数据 API）
=================================================
替代 akshare，避免 GitHub Actions 云端 IP 被东方财富断连/限流。

Tushare API 文档: https://tushare.pro/document/2
需要环境变量: TUSHARE_TOKEN（在 GitHub Secrets 中配置）
"""
import os
import time
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
TS_RETRY_TIMES = 3         # 单次 Tushare 调用重试次数（应对网络抖动/限流）
TS_RETRY_WAIT = 3          # 重试间隔基数（秒），第 n 次重试等待 n * TS_RETRY_WAIT
MAX_FALLBACK_DAYS = 5      # 数据不可用时，最多向前回退的交易日数
MIN_EXPECTED_ROWS = 1000   # 单日全市场行情的最小有效条数（低于此值视为异常/未发布）

# 模块级缓存：本次运行统一使用的行情日期。
# fetch_market_spot 回退后会把最终采用日期写进来，后续的
# 资金流 / 大盘 / 复盘取数复用同一日期，避免各接口口径错位。
_RESOLVED_TRADE_DATE: str = ''


def _ts_call(func: Any, **kwargs) -> Any:
    """带重试的 Tushare 调用；全部失败返回 None（不抛异常，由调用方决定降级策略）"""
    last_err = None
    for attempt in range(1, TS_RETRY_TIMES + 1):
        try:
            return func(**kwargs)
        except Exception as e:
            last_err = e
            if attempt < TS_RETRY_TIMES:
                time.sleep(TS_RETRY_WAIT * attempt)
    print(f'  ⚠️ Tushare 调用失败（已重试 {TS_RETRY_TIMES} 次）: {last_err}')
    return None


def _trading_days_desc(pro: Any, count: int = 70) -> List[str]:
    """返回最近 count 个交易日，按日期倒序（下标 0 为最近一个交易日）"""
    end = datetime.now()
    start = end - timedelta(days=count * 2 + 40)
    cal = _ts_call(
        pro.trade_cal, exchange='SSE', is_open='1',
        start_date=start.strftime('%Y%m%d'), end_date=end.strftime('%Y%m%d'),
    )
    if cal is None or cal.empty:
        return []
    return cal.sort_values('cal_date', ascending=False)['cal_date'].tolist()[:count]


def _probe_published(pro: Any, trade_date: str) -> bool:
    """探查某个交易日的日线数据是否已经发布（Tushare 收盘后约 17:00-19:00 才更新）"""
    probe = _ts_call(pro.daily, trade_date=trade_date, fields='ts_code')
    return probe is not None and not probe.empty


def _find_anchor(pro: Any, all_dates: List[str], max_probe: int = MAX_FALLBACK_DAYS) -> int:
    """从最近交易日向前探查，返回第一个「日线数据已发布」的索引；找不到时返回 0"""
    for i in range(min(max_probe, len(all_dates))):
        if _probe_published(pro, all_dates[i]):
            if i > 0:
                skipped = '、'.join(all_dates[:i])
                print(f'  ⏮️ 跳过无数据/未发布的交易日 {skipped}，'
                      f'回退到最近已发布交易日 {all_dates[i]}')
            return i
    return 0


def _get_trade_dates(pro: Any, days_back_list: List[int]) -> Dict[int, str]:
    """获取 N 个交易日前的日期，返回 {N: 'YYYYMMDD'}

    ⚠️ 关键点：N=0 表示「最近一个日线数据『已发布』的交易日」，而不是日历上的最近交易日。
    Tushare 的日线数据要等收盘后（约 17:00-19:00）才更新，所以：
      - 早盘 08:35 运行时，当天数据还没有 → 必须回退到上一交易日
      - 复盘若在 18:30 运行，当天数据可能尚未就绪 → 同样回退到上一交易日
    否则会取到空数据，导致主流程 exit(1)。
    """
    need = max(days_back_list) + MAX_FALLBACK_DAYS + 5
    all_dates = _trading_days_desc(pro, count=need)
    if not all_dates:
        raise RuntimeError('未找到交易日历')
    anchor = _find_anchor(pro, all_dates)

    # 若本次运行已由 fetch_market_spot 决议过日期（含回退），统一复用之
    if _RESOLVED_TRADE_DATE and _RESOLVED_TRADE_DATE in all_dates:
        anchor = all_dates.index(_RESOLVED_TRADE_DATE)

    usable = all_dates[anchor:]
    result = {}
    for n in days_back_list:
        if n < len(usable):
            result[n] = usable[n]
    if not result:
        raise RuntimeError('未找到可用的交易日（数据可能全部未发布）')
    return result


def _get_stock_names(pro: Any) -> pd.DataFrame:
    """获取全部上市股票代码-名称映射（带重试）"""
    basic = _ts_call(
        pro.stock_basic, exchange='', list_status='L',
        fields='ts_code,symbol,name,market,industry',
    )
    return basic


_INDUSTRY_MAP_CACHE: Dict[str, str] = {}


def fetch_industry_map() -> Dict[str, str]:
    """获取「6 位股票代码 → Tushare 行业分类」映射（模块级缓存，全天复用）。

    用途：板块归类的**行业兜底**。
    HOT_THEMES 靠股票名称关键词匹配（655 个关键词），实测全市场覆盖率仅约 13%
    （705/5550，2026-09-16）→ 大多数票落在「其他」无法参与板块统计。
    加上行业兜底后，板块统计分母接近全市场。

    失败返回空 dict（调用方自动回退到纯题材匹配，不阻塞主流程）。
    """
    global _INDUSTRY_MAP_CACHE
    if _INDUSTRY_MAP_CACHE:
        return _INDUSTRY_MAP_CACHE
    try:
        pro = _init_tushare()
        basic = _ts_call(pro.stock_basic, exchange='', list_status='L',
                         fields='ts_code,name,industry')
        m: Dict[str, str] = {}
        if basic is not None and not basic.empty:
            for ts_code, ind in zip(basic['ts_code'], basic['industry']):
                code = str(ts_code).split('.')[0].zfill(6)
                s = str(ind).strip()
                if code and s and s.lower() != 'nan':
                    m[code] = s
        if m:
            _INDUSTRY_MAP_CACHE = m
            print(f'  ✅ 行业分类映射: {len(m)} 只（板块归类行业兜底已启用）')
        else:
            print('  ⚠️ 行业分类映射为空 → 板块归类回退纯题材匹配')
    except Exception as e:
        print(f'  ⚠️ 行业分类映射获取失败（回退纯题材匹配）: {e}')
    return _INDUSTRY_MAP_CACHE


# ============ 行情抓取 ============
def fetch_market_spot() -> pd.DataFrame:
    """
    获取全市场行情（基于最近「数据已发布」交易日的 EOD 数据）

    容错设计（针对 08:35 早盘场景：当日数据可能尚未发布，或临时读取失败）：
    1. 从最近交易日向前探查，跳过「日线数据尚未发布」的日期；
    2. 逐日构建行情；若某日构建失败或行情条数异常偏低（< MIN_EXPECTED_ROWS），
       自动再向前回退一个交易日重试；
    3. 最多回退 MAX_FALLBACK_DAYS 个交易日，全部失败才返回空 DataFrame。

    字段：code, name, price, pct_change, pct_5d, pct_60d, open,
          turnover_rate, pe_ttm, pb, total_mcap, circ_mcap,
          volume_ratio, volume, turnover, high, low, pre_close, change
    """
    print('📡 抓取全市场行情（Tushare）...')
    global _RESOLVED_TRADE_DATE
    try:
        pro = _init_tushare()
    except Exception as e:
        print(f'  ❌ Tushare 初始化失败: {e}')
        return pd.DataFrame()

    all_dates = _trading_days_desc(pro, count=60 + MAX_FALLBACK_DAYS + 10)
    if not all_dates:
        print('  ❌ 未取得交易日历')
        return pd.DataFrame()

    anchor = _find_anchor(pro, all_dates)

    for extra in range(0, MAX_FALLBACK_DAYS + 1):
        idx = anchor + extra
        if idx + 60 >= len(all_dates):
            print('  ⚠️ 可用交易日不足 60 个，停止回退')
            break

        today, d_5d, d_60d = all_dates[idx], all_dates[idx + 5], all_dates[idx + 60]
        if extra > 0:
            print(f'  ⏮️ 回退 {extra} 个交易日，改用 {today} 数据（5日前={d_5d} | 60日前={d_60d}）')

        df = _build_spot(pro, today, d_5d, d_60d)
        got = 0 if df is None else len(df)
        if got >= MIN_EXPECTED_ROWS:
            print(f'  ✅ 行情就绪：{today}（{got} 条）')
            _RESOLVED_TRADE_DATE = today
            return df
        print(f'  ⚠️ {today} 行情异常（{got} 条 < {MIN_EXPECTED_ROWS}），尝试回退前一交易日')

    print('  ❌ 连续回退后仍无法取得有效行情')
    return pd.DataFrame()


def _build_spot(pro: Any, today: str, d_5d: str, d_60d: str) -> pd.DataFrame:
    """构建指定交易日的全市场行情（供 fetch_market_spot 逐日回退重试）"""
    try:
        # 1. 当日日线行情
        daily_today = _ts_call(pro.daily, trade_date=today)
        if daily_today is None or daily_today.empty:
            print(f'  ⚠️ {today} 当日行情为空')
            return pd.DataFrame()
        print(f'  ✅ {today} 当日行情: {len(daily_today)} 条')

        # 2. 当日估值基础
        basic_today = _ts_call(
            pro.daily_basic, trade_date=today,
            fields='ts_code,trade_date,pe,pe_ttm,pb,total_mv,circ_mv,turnover_rate,volume_ratio',
        )
        if basic_today is None or basic_today.empty:
            print(f'  ⚠️ {today} 估值数据为空')
            return pd.DataFrame()
        print(f'  ✅ {today} 当日估值: {len(basic_today)} 条')

        # 3. 股票名称映射
        stock_names = _get_stock_names(pro)
        if stock_names is None or stock_names.empty:
            print(f'  ⚠️ {today} 股票名称映射为空')
            return pd.DataFrame()
        name_map = stock_names.set_index('ts_code')['name'].to_dict()

        # 4. 5d / 60d 收盘价（用于算趋势涨幅）
        def _close_on(date_str: str, out_col: str) -> pd.DataFrame:
            d = _ts_call(pro.daily, trade_date=date_str, fields='ts_code,close')
            if d is None or d.empty:
                print(f'  ⚠️ {date_str} 无行情数据，{out_col} 记为空')
                return pd.DataFrame(columns=['ts_code', out_col])
            return d[['ts_code', 'close']].rename(columns={'close': out_col})

        daily_5d = _close_on(d_5d, 'close_5d')
        daily_60d = _close_on(d_60d, 'close_60d')

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
        print(f'  ❌ 构建 {today} 行情异常: {e}')
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

        df = _ts_call(pro.moneyflow, trade_date=trade_date)
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


# ============ 均线面板（趋势线 MA120）============
# 背景：技术面原来是纯行情快照打分（当日涨幅 / 5日涨幅 / 60日趋势 / 量比），
#       **完全不含均线** → 「技术面高分」可能只是短期涨得猛。
#       2026-09-22 起新增「趋势线」项：以 MA120 为趋势锚，
#       MA5>MA120 且现价≥MA120 → 满分；MA5>MA20 → 及格。
# 取数方式：**按 trade_date 逐日拉全市场日线**（约 130 次调用 ≈ 145 秒），
#       而不是逐股调 fetch_history_kline（5500+ 次，云端不可接受）。
# 关断开关：环境变量 MA120_TREND=off → 不取数、不并表 → selector 自动退回原打分。
MA_TREND_ENABLED = os.environ.get('MA120_TREND', 'on').strip().lower() not in ('0', 'off', 'false', 'no')
MA_TREND_DAYS = 130          # MA120 需 ≥120 根；留 10 根缓冲（停牌/新股）
_MA_PANEL_CACHE: pd.DataFrame = pd.DataFrame()


def fetch_ma_panel(need: int = MA_TREND_DAYS) -> pd.DataFrame:
    """批量计算全市场 MA5 / MA20 / MA120（模块级缓存，一次运行只算一次）。

    返回 DataFrame[code, ma5, ma20, ma120]（code 为 6 位字符串）：
      - 均线值按「有效收盘价根数」设门槛：ma5 需 ≥5 根、ma20 需 ≥20 根、ma120 需 ≥120 根；
        不足者置 NaN（次新股即落在此列）。
    失败 / 关闭开关 → 返回空 DataFrame（调用方自动退回原打分，不阻塞主流程）。
    """
    global _MA_PANEL_CACHE
    if not MA_TREND_ENABLED:
        print('  ⏭️ 趋势线(MA120)已关闭（MA120_TREND=off）→ 技术面沿用原 8/8/5/4 口径')
        return pd.DataFrame()
    if not _MA_PANEL_CACHE.empty:
        return _MA_PANEL_CACHE
    try:
        pro = _init_tushare()
    except Exception as e:
        print(f'  ⚠️ 趋势线取数跳过（Tushare 未就绪）: {e}')
        return pd.DataFrame()

    # 1) 交易日窗口：右端对齐「已发布的最近交易日」（与全套取数同一口径）
    dates_desc = _trading_days_desc(pro, count=need + 15)
    if not dates_desc:
        print('  ⚠️ 趋势线取数失败：未取到交易日历')
        return pd.DataFrame()
    if _RESOLVED_TRADE_DATE and _RESOLVED_TRADE_DATE in dates_desc:
        right = dates_desc.index(_RESOLVED_TRADE_DATE)
    else:
        right = _find_anchor(pro, dates_desc)
    window = dates_desc[right:right + need]        # 倒序，[0] 为最近交易日
    if len(window) < 120:
        print(f'  ⚠️ 趋势线取数失败：有效交易日仅 {len(window)} 天（需 ≥120）')
        return pd.DataFrame()

    # 2) 逐日拉全市场日线（批量，非逐股）
    print(f'  📡 趋势线取数：{len(window)} 个交易日 × 全市场日线（批量）...')
    t0 = time.time()
    frames = []
    got = 0
    for i, d in enumerate(window):
        one = _ts_call(pro.daily, trade_date=d, fields='ts_code,close')
        if one is not None and not one.empty:
            one = one[['ts_code', 'close']].copy()
            one['trade_date'] = d
            frames.append(one)
            got += 1
        if (i + 1) % 30 == 0:
            print('     ...趋势线 %d/%d 日（已获取 %d 日）'
                  % (i + 1, len(window), got))
    if got < 120 or not frames:
        print(f'  ⚠️ 趋势线取数失败：仅 {got}/{len(window)} 个交易日有数据')
        return pd.DataFrame()
    if got < len(window):
        print('  ⚠️ 趋势线：%d/%d 个交易日缺数据（缺失日的股票该日不计入均线，'
              '样本仍 ≥120 根故可继续）' % (len(window) - got, len(window)))

    # 3) 透视成「日期 × 代码」收盘价矩阵 → 直接算均线
    pv = pd.concat(frames, ignore_index=True).pivot_table(
        index='trade_date', columns='ts_code', values='close', aggfunc='last')
    if pv.empty:
        print('  ⚠️ 趋势线取数失败：透视后为空')
        return pd.DataFrame()

    valid = pv.notna().sum()
    ma5 = pv.tail(5).mean().where(valid >= 5)
    ma20 = pv.tail(20).mean().where(valid >= 20)
    ma120 = pv.tail(120).mean().where(valid >= 120)

    out = pd.DataFrame({
        'code': [str(c).split('.')[0].zfill(6) for c in pv.columns],
        'ma5': ma5.values,
        'ma20': ma20.values,
        'ma120': ma120.values,
    })
    out = out[out['ma120'].notna()].reset_index(drop=True)
    if out.empty:
        print('  ⚠️ 趋势线取数失败：无任何股票满足 120 根有效收盘价')
        return pd.DataFrame()

    _MA_PANEL_CACHE = out
    print('  ✅ 趋势线取数完成：%d 只含 MA120，用时 %.0f 秒（%d 个交易日）'
          % (len(out), time.time() - t0, got))
    return out


def merge_ma_panel(df: pd.DataFrame) -> pd.DataFrame:
    """把均线面板并进候选表（新增 ma5 / ma20 / ma120 三列）。

    取不到数据时**原样返回**（不新增列）→ selector 的 _ma_trend_tier 返回 None
    → 技术面自动退回原 8/8/5/4 口径，与改版前行为一致。
    """
    if df is None or df.empty:
        return df
    panel = fetch_ma_panel()
    if panel is None or panel.empty:
        return df
    df = df.copy()
    df['code_str'] = df['code'].astype(str).str.zfill(6)
    df = df.merge(panel[['code', 'ma5', 'ma20', 'ma120']],
                  left_on='code_str', right_on='code', how='left', suffixes=('', '_ma'))
    hit = int(df['ma120'].notna().sum()) if 'ma120' in df.columns else 0
    print(f'  ✅ 均线面板已并入：{hit}/{len(df)} 只含 MA120')
    return df


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
        idx_today = _ts_call(pro.index_daily, ts_code='000001.SH', trade_date=trade_date)
        if idx_today is not None and not idx_today.empty:
            row = idx_today.iloc[0]
            result['index_close'] = float(row['close'])
            result['index_pct_change'] = float(row['pct_chg'])
            print(f'  ✅ 上证指数: {row["close"]:.2f} ({row["pct_chg"]:+.2f}%)')
        else:
            print('  ⚠️ 上证指数当日行情为空')

        # 2. 上证指数60日 K线 → MA20/MA60 趋势判断
        start_date = (datetime.now() - timedelta(days=130)).strftime('%Y%m%d')
        idx_hist = _ts_call(
            pro.index_daily, ts_code='000001.SH',
            start_date=start_date, end_date=trade_date,
        )
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
        mf = _ts_call(pro.moneyflow, trade_date=trade_date)
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


def fetch_market_review() -> Dict[str, Any]:
    """
    获取盘后复盘所需的大盘环境 + 市场情绪数据（全部基于 Tushare）：
    - index_close: 上证指数收盘点位
    - index_pct_change: 当日涨跌幅（%）
    - main_net_inflow_yi: 大盘主力净额（亿元）
    - trend_60d / trend_detail: 60日趋势分类
    - amount_yi: 两市成交总额（亿元）
    - limit_up_count: 涨停家数（约，pct_chg >= 9.8）
    - limit_down_count: 跌停家数（约，pct_chg <= -9.8）
    - up_count / down_count: 上涨/下跌家数
    - data_date: 数据日期
    """
    print('📡 抓取盘后复盘（大盘环境 + 市场情绪）...')
    result: Dict[str, Any] = {
        'index_close': None,
        'index_pct_change': None,
        'main_net_inflow_yi': None,
        'trend_60d': 'unknown',
        'trend_detail': '',
        'amount_yi': None,
        'limit_up_count': None,
        'limit_down_count': None,
        'up_count': None,
        'down_count': None,
        'data_date': '',
    }
    try:
        pro = _init_tushare()
        trade_date = _get_trade_dates(pro, [0])[0]
        result['data_date'] = trade_date

        # 1. 上证指数当日行情
        try:
            idx_today = _ts_call(pro.index_daily, ts_code='000001.SH', trade_date=trade_date)
            if idx_today is not None and not idx_today.empty:
                row = idx_today.iloc[0]
                result['index_close'] = float(row['close'])
                result['index_pct_change'] = float(row['pct_chg'])
                print(f'  ✅ 上证指数: {row["close"]:.2f} ({row["pct_chg"]:+.2f}%)')
            else:
                print('  ⚠️ 上证指数当日行情为空')
        except Exception as e:
            print(f'  ⚠️ 上证指数失败: {e}')

        # 2. 上证指数60日 K线 → MA20/MA60 趋势判断
        try:
            start_date = (datetime.now() - timedelta(days=130)).strftime('%Y%m%d')
            idx_hist = _ts_call(
                pro.index_daily, ts_code='000001.SH',
                start_date=start_date, end_date=trade_date,
            )
            if idx_hist is not None and len(idx_hist) >= 60:
                closes = idx_hist.sort_values('trade_date')['close'].astype(float).values
                closes = closes[-60:]
                ma20 = float(closes[-20:].mean())
                ma60 = float(closes.mean())
                ma_diff_pct = (ma20 - ma60) / ma60 * 100
                current = float(closes[-1])
                if ma_diff_pct > 2.0 and current > ma20:
                    result['trend_60d'] = 'up'
                    emoji, label = '📈', '上涨趋势'
                elif ma_diff_pct < -2.0 and current < ma20:
                    result['trend_60d'] = 'down'
                    emoji, label = '📉', '下跌趋势'
                else:
                    result['trend_60d'] = 'sideways'
                    emoji, label = '〰️', '震荡整理'
                result['trend_detail'] = (
                    f'{emoji}{label} '
                    f'(MA20={ma20:.0f} vs MA60={ma60:.0f}, 偏离 {ma_diff_pct:+.1f}%)'
                )
                print(f'  ✅ 60日趋势: {result["trend_detail"]}')
            else:
                print(f'  ⚠️ 上证指数60日数据不足（仅 {0 if idx_hist is None else len(idx_hist)} 条）')
        except Exception as e:
            print(f'  ⚠️ 60日趋势失败: {e}')

        # 3. 全市场主力净额（moneyflow 大单+特大单，单位万→亿）
        try:
            mf = _ts_call(pro.moneyflow, trade_date=trade_date)
            if mf is not None and not mf.empty:
                big_net_wan = (
                    mf['buy_lg_amount'].fillna(0).sum()
                    + mf['buy_elg_amount'].fillna(0).sum()
                    - mf['sell_lg_amount'].fillna(0).sum()
                    - mf['sell_elg_amount'].fillna(0).sum()
                )
                result['main_net_inflow_yi'] = big_net_wan / 1e4
                print(f'  ✅ 大盘主力净额: {result["main_net_inflow_yi"]:+.1f} 亿')
            else:
                print('  ⚠️ 资金流数据为空')
        except Exception as e:
            print(f'  ⚠️ 大盘主力净额失败: {e}')

        # 4. 全市场日线：成交额 + 涨跌停家数 + 涨跌家数
        try:
            daily_today = _ts_call(pro.daily, trade_date=trade_date)
            if daily_today is not None and not daily_today.empty:
                # 成交额：Tushare daily 的 amount 单位为千元 → 元×1000 → 亿/1e5
                result['amount_yi'] = daily_today['amount'].sum() / 1e5
                pct = daily_today['pct_chg'].fillna(0)
                result['limit_up_count'] = int((pct >= 9.8).sum())
                result['limit_down_count'] = int((pct <= -9.8).sum())
                result['up_count'] = int((pct > 0).sum())
                result['down_count'] = int((pct < 0).sum())
                print(f"  ✅ 两市成交额: {result['amount_yi']:.0f} 亿 | "
                      f"涨停 {result['limit_up_count']} | 跌停 {result['limit_down_count']} | "
                      f"涨 {result['up_count']} | 跌 {result['down_count']}")
            else:
                print('  ⚠️ 全市场日线为空')
        except Exception as e:
            print(f'  ⚠️ 市场情绪抓取失败: {e}')

    except Exception as e:
        print(f'  ❌ 抓取盘后复盘失败: {e}')
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
