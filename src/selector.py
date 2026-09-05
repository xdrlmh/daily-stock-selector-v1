"""
筛选与五维评分模块
评分维度（满分 100）：
- 技术面 25%
- 资金面 20%
- 估值吸引力 15%
- 事件催化 15%
- 基本面 25%
"""
import pandas as pd
import numpy as np
from typing import List, Dict, Tuple, Optional
from .config import SCORE_WEIGHTS, CONFIG
from .data_fetcher import fetch_history_kline


# ============================================================
# 一、技术面评分（25 分）
# ============================================================
def calc_tech_score(row: pd.Series) -> Tuple[float, Dict]:
    """
    基于实时行情粗略打分（不依赖 K 线）
    - 当日涨幅（温和上涨 0~5% 加分，过热扣分）
    - 5日累计涨幅（趋势强度）
    - 60日涨幅（中长期趋势）
    - 量比（量能配合）
    """
    score = 0
    detail = {}

    # 1) 当日涨幅（0~8 分）
    pct = row.get('pct_change', 0)
    if pd.isna(pct):
        pct = 0
    if 0 <= pct <= 3:
        score += 8; detail['当日涨幅'] = f'{pct:.1f}% (8分)'
    elif 3 < pct <= 6:
        score += 6; detail['当日涨幅'] = f'{pct:.1f}% (6分)'
    elif 6 < pct <= 9.5:
        score += 4; detail['当日涨幅'] = f'{pct:.1f}% (4分)'
    elif pct > 9.5:  # 涨停
        score += 2; detail['当日涨幅'] = f'{pct:.1f}% 涨停 (2分)'
    elif -2 <= pct < 0:
        score += 4; detail['当日涨幅'] = f'{pct:.1f}% 微调 (4分)'
    else:
        score += 1; detail['当日涨幅'] = f'{pct:.1f}% 下跌 (1分)'

    # 2) 5日累计涨幅（0~8 分）
    pct_5d = row.get('pct_5d', row.get('pct_change', 0))  # 没 5日数据时用当日
    if pd.isna(pct_5d):
        pct_5d = 0
    if 3 <= pct_5d <= 15:
        score += 8; detail['5日涨幅'] = f'{pct_5d:.1f}% (8分)'
    elif 15 < pct_5d <= 30:
        score += 5; detail['5日涨幅'] = f'{pct_5d:.1f}% (5分)'
    elif pct_5d > 30:
        score += 2; detail['5日涨幅'] = f'{pct_5d:.1f}% 过热 (2分)'
    else:
        score += 3; detail['5日涨幅'] = f'{pct_5d:.1f}% (3分)'

    # 3) 中长期趋势 - 60日涨幅（0~5 分）
    pct_60d = row.get('pct_60d', 0)
    if pd.isna(pct_60d):
        pct_60d = 0
    if pct_60d > 10:
        score += 5; detail['60日趋势'] = f'{pct_60d:.1f}% (5分)'
    elif pct_60d > 0:
        score += 3; detail['60日趋势'] = f'{pct_60d:.1f}% (3分)'
    elif pct_60d > -10:
        score += 1; detail['60日趋势'] = f'{pct_60d:.1f}% (1分)'
    else:
        score += 0; detail['60日趋势'] = f'{pct_60d:.1f}% (0分)'

    # 4) 量比（0~4 分）
    vr = row.get('volume_ratio', 0)
    if pd.isna(vr):
        vr = 0
    if 1.5 <= vr <= 4:
        score += 4; detail['量比'] = f'{vr:.2f} (4分)'
    elif 1 <= vr < 1.5:
        score += 2; detail['量比'] = f'{vr:.2f} (2分)'
    elif vr > 4:
        score += 2; detail['量比'] = f'{vr:.2f} 巨量 (2分)'
    else:
        score += 0; detail['量比'] = f'{vr:.2f} (0分)'

    return min(score, SCORE_WEIGHTS['technical']), detail


# ============================================================
# 二、资金面评分（20 分）
# ============================================================
def calc_capital_score(row: pd.Series) -> Tuple[float, Dict]:
    """主力资金净流入 + 换手率合理性"""
    score = 0
    detail = {}

    # 1) 主力净流入（0~14 分）
    inflow = row.get('main_net_inflow', 0)
    if pd.isna(inflow):
        inflow = 0
    inflow_yi = inflow / 1e8  # 转亿

    if inflow_yi >= 5:
        score += 14; detail['主力净流入'] = f'+{inflow_yi:.1f}亿 (14分)'
    elif inflow_yi >= 1:
        score += 10; detail['主力净流入'] = f'+{inflow_yi:.1f}亿 (10分)'
    elif inflow_yi >= 0.3:
        score += 6; detail['主力净流入'] = f'+{inflow_yi:.1f}亿 (6分)'
    elif inflow_yi > 0:
        score += 3; detail['主力净流入'] = f'+{inflow_yi:.1f}亿 (3分)'
    elif inflow_yi > -0.3:
        score += 1; detail['主力净流入'] = f'{inflow_yi:.1f}亿 (1分)'
    else:
        score += 0; detail['主力净流入'] = f'{inflow_yi:.1f}亿 (0分)'

    # 2) 换手率（0~6 分）
    turnover = row.get('turnover_rate', 0)
    if pd.isna(turnover):
        turnover = 0
    if 3 <= turnover <= 10:
        score += 6; detail['换手率'] = f'{turnover:.1f}% (6分)'
    elif 10 < turnover <= 20:
        score += 3; detail['换手率'] = f'{turnover:.1f}% (3分)'
    elif turnover > 20:
        score += 1; detail['换手率'] = f'{turnover:.1f}% 过热 (1分)'
    else:
        score += 2; detail['换手率'] = f'{turnover:.1f}% (2分)'

    return min(score, SCORE_WEIGHTS['capital']), detail


# ============================================================
# 三、估值吸引力（15 分）
# ============================================================
def calc_valuation_score(row: pd.Series) -> Tuple[float, Dict]:
    """PE/PB 估值打分"""
    score = 0
    detail = {}

    pe = row.get('pe_ttm', 0)
    pb = row.get('pb', 0)

    # 1) PE-TTM（0~10 分）
    if pd.isna(pe) or pe <= 0:
        score += 0; detail['PE-TTM'] = '亏损或缺失 (0分)'
    elif pe < 0:
        score += 0; detail['PE-TTM'] = '亏损 (0分)'
    elif pe < 15:
        score += 10; detail['PE-TTM'] = f'{pe:.1f} (10分)'
    elif pe < 25:
        score += 7; detail['PE-TTM'] = f'{pe:.1f} (7分)'
    elif pe < 50:
        score += 4; detail['PE-TTM'] = f'{pe:.1f} (4分)'
    elif pe < 100:
        score += 2; detail['PE-TTM'] = f'{pe:.1f} (2分)'
    else:
        score += 1; detail['PE-TTM'] = f'{pe:.1f} 高估 (1分)'

    # 2) PB（0~5 分）
    if pd.isna(pb) or pb <= 0:
        score += 0; detail['PB'] = '缺失 (0分)'
    elif pb < 1:
        score += 5; detail['PB'] = f'{pb:.2f} 破净 (5分)'
    elif pb < 3:
        score += 4; detail['PB'] = f'{pb:.2f} (4分)'
    elif pb < 6:
        score += 2; detail['PB'] = f'{pb:.2f} (2分)'
    else:
        score += 1; detail['PB'] = f'{pb:.2f} (1分)'

    return min(score, SCORE_WEIGHTS['valuation']), detail


# ============================================================
# 四、事件/题材催化（15 分）
# ============================================================
# 热门主题关键词（根据近期市场热点维护）
HOT_THEMES = {
    '农林牧渔': ['牧原', '温氏', '新希望', '海大', '大北农', '金新农', '正邦', '天邦', '益生', '民和', '圣农', '仙坛', '益生股份', '圣农发展', '京基智农', '天康', '神农', '傲农', '华统', '唐人神'],
    '人工智能': ['科大讯飞', '中科曙光', '浪潮', '拓尔思', '汉王', '海康', '大华', '云从', '寒武纪', '商汤', '科大', '讯飞', '同方', '紫光'],
    '新能源车': ['比亚迪', '宁德', '亿纬', '赣锋', '天齐', '华友', '当升', '容百', '星源', '恩捷', '先导', '璞泰来', '杉杉'],
    '半导体': ['中芯', '韦尔', '卓胜', '兆易', '长电', '通富', '华天', '北方华创', '中微', '沪硅', '雅克', '彤程', '晶瑞', '江丰'],
    '医药': ['恒瑞', '药明', '智飞', '迈瑞', '爱尔', '通策', '片仔癀', '云南白药', '同仁堂', '东阿', '复星', '华东医药'],
}


def calc_catalyst_score(row: pd.Series) -> Tuple[float, Dict]:
    """基于股票名称粗略判断题材热度"""
    score = 0
    detail = {}
    name = row.get('name', '')

    matched_themes = []
    for theme, keywords in HOT_THEMES.items():
        for kw in keywords:
            if kw in name:
                matched_themes.append(theme)
                break

    if matched_themes:
        score += 10
        detail['题材'] = f'{matched_themes[0]} (10分)'
    else:
        score += 3
        detail['题材'] = '无明确热点 (3分)'

    # 当日涨幅 > 5 加分（市场关注度高）
    pct = row.get('pct_change', 0)
    if pd.isna(pct):
        pct = 0
    if pct > 5:
        score += 5; detail['当日关注'] = f'+{pct:.1f}% (5分)'
    elif pct > 2:
        score += 3; detail['当日关注'] = f'+{pct:.1f}% (3分)'
    else:
        score += 0; detail['当日关注'] = f'+{pct:.1f}% (0分)'

    return min(score, SCORE_WEIGHTS['catalyst']), detail


# ============================================================
# 五、基本面（25 分）
# ============================================================
def calc_fundamental_score(row: pd.Series) -> Tuple[float, Dict]:
    """基于 PE + 市值 + 涨跌幅间接打分"""
    score = 0
    detail = {}

    pe = row.get('pe_ttm', 0)
    mcap = row.get('circ_mcap', 0)

    # 1) 业绩为正（PE > 0）（0~12 分）
    if pd.isna(pe):
        pe = 0
    if 0 < pe < 30:
        score += 12; detail['业绩'] = f'PE {pe:.1f} 健康 (12分)'
    elif 0 < pe < 60:
        score += 8; detail['业绩'] = f'PE {pe:.1f} (8分)'
    elif 0 < pe < 100:
        score += 4; detail['业绩'] = f'PE {pe:.1f} 偏高 (4分)'
    elif pe >= 100:
        score += 2; detail['业绩'] = f'PE {pe:.1f} 高估 (2分)'
    else:
        score += 0; detail['业绩'] = '亏损 (0分)'

    # 2) 市值合理性（0~8 分）
    if pd.isna(mcap):
        mcap = 0
    mcap_yi = mcap / 1e8
    if 50 <= mcap_yi <= 500:
        score += 8; detail['市值'] = f'{mcap_yi:.0f}亿 适中 (8分)'
    elif 30 <= mcap_yi < 50:
        score += 5; detail['市值'] = f'{mcap_yi:.0f}亿 小盘 (5分)'
    elif 500 < mcap_yi <= 1500:
        score += 5; detail['市值'] = f'{mcap_yi:.0f}亿 中大盘 (5分)'
    elif mcap_yi > 1500:
        score += 2; detail['市值'] = f'{mcap_yi:.0f}亿 超大盘 (2分)'
    else:
        score += 1; detail['市值'] = f'{mcap_yi:.0f}亿 (1分)'

    # 3) 趋势稳定性（0~5 分）
    pct_60d = row.get('pct_60d', 0)
    if pd.isna(pct_60d):
        pct_60d = 0
    if -20 <= pct_60d <= 30:
        score += 5; detail['稳定性'] = f'60日 {pct_60d:.1f}% 平稳 (5分)'
    elif pct_60d > 30:
        score += 2; detail['稳定性'] = f'60日 +{pct_60d:.1f}% 急涨 (2分)'
    else:
        score += 0; detail['稳定性'] = f'60日 {pct_60d:.1f}% 弱势 (0分)'

    return min(score, SCORE_WEIGHTS['fundamental']), detail


# ============================================================
# 总评分
# ============================================================
def calc_total_score(row: pd.Series) -> Tuple[float, Dict]:
    """计算五维评分总和"""
    tech_score, tech_detail = calc_tech_score(row)
    capital_score, capital_detail = calc_capital_score(row)
    val_score, val_detail = calc_valuation_score(row)
    cat_score, cat_detail = calc_catalyst_score(row)
    fund_score, fund_detail = calc_fundamental_score(row)

    total = tech_score + capital_score + val_score + cat_score + fund_score

    breakdown = {
        '技术面': f'{tech_score:.0f}/{SCORE_WEIGHTS["technical"]}',
        '资金面': f'{capital_score:.0f}/{SCORE_WEIGHTS["capital"]}',
        '估值': f'{val_score:.0f}/{SCORE_WEIGHTS["valuation"]}',
        '催化': f'{cat_score:.0f}/{SCORE_WEIGHTS["catalyst"]}',
        '基本面': f'{fund_score:.0f}/{SCORE_WEIGHTS["fundamental"]}',
        **tech_detail,
        **capital_detail,
        **val_detail,
        **cat_detail,
        **fund_detail,
    }
    return total, breakdown


# ============================================================
# 风险过滤
# ============================================================
def is_overbought(row: pd.Series) -> bool:
    """判断是否高位派发段"""
    pct_5d = row.get('pct_5d', row.get('pct_change', 0))
    pct_10d = row.get('pct_10d', row.get('pct_change', 0))
    if pd.isna(pct_5d): pct_5d = 0
    if pd.isna(pct_10d): pct_10d = 0
    if pct_5d >= CONFIG['high_rise_threshold_5d']:
        return True
    if pct_10d >= CONFIG['high_rise_threshold_10d']:
        return True
    return False


def is_overheated_turnover(row: pd.Series) -> bool:
    """换手过热"""
    t = row.get('turnover_rate', 0)
    if pd.isna(t):
        return False
    return t >= CONFIG['turnover_overheat']


def is_capital_outflow(row: pd.Series) -> bool:
    """主力大幅净流出"""
    inflow_pct = row.get('main_net_inflow_pct', 0)
    if pd.isna(inflow_pct):
        return False
    # 净占比 < -5% 视为派发
    return inflow_pct < -CONFIG['main_inflow_ratio_threshold'] * 100


def screen_stocks(df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    主筛选函数：返回 (TOP 5, 警示名单, 其余候选)
    """
    if df.empty:
        return df, df, df

    print(f'\n🔍 开始评分 {len(df)} 只候选股票...')

    # 计算每只股票的总分
    scores = []
    breakdowns = []
    for idx, row in df.iterrows():
        total, breakdown = calc_total_score(row)
        scores.append(total)
        breakdowns.append(breakdown)

    df = df.copy()
    df['total_score'] = scores
    df['score_breakdown'] = breakdowns

    # 按总分降序
    df_sorted = df.sort_values('total_score', ascending=False).reset_index(drop=True)

    # 风险过滤
    warnings_mask = df_sorted.apply(
        lambda r: is_overbought(r) or is_overheated_turnover(r) or is_capital_outflow(r),
        axis=1
    )

    # TOP 5：风险标的剔除后取前 5
    candidates = df_sorted[~warnings_mask].head(CONFIG['max_picks'])
    # 警示名单：风险标的取前 5
    warning_list = df_sorted[warnings_mask].head(CONFIG['max_warnings'])

    print(f'  ✅ TOP 精选：{len(candidates)} 只')
    print(f'  ⚠️  警示名单：{len(warning_list)} 只')

    return candidates, warning_list, df_sorted


# ============================================================
# 兜底：如果候选不足，从涨跌幅榜补齐
# ============================================================
def fallback_from_top_gainers(df: pd.DataFrame, n: int = 5) -> pd.DataFrame:
    """从涨跌幅榜前 N 取候选"""
    if df.empty:
        return df
    top = df.sort_values('pct_change', ascending=False).head(n * 3)
    for idx, row in top.iterrows():
        total, breakdown = calc_total_score(row)
        top.at[idx, 'total_score'] = total
        top.at[idx, 'score_breakdown'] = breakdown
    return top.sort_values('total_score', ascending=False).head(n)


if __name__ == '__main__':
    print('评分模块独立测试需要行情数据，请通过 main.py 运行')