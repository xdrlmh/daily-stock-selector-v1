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
from .config import (SCORE_WEIGHTS, CONFIG, CAPITAL_MODE, CAPITAL_TREND_FILTER,
                     CAPITAL_MIN_VALID_DAYS, CAPITAL_MISSING_ZERO_SCOPE,
                     CAPITAL_ELG_FULL, CAPITAL_ELG_PARTIAL,
                     CAPITAL_ELG_NONE, CAPITAL_RATIO_BANDS)
from .data_fetcher import fetch_history_kline


# ============================================================
# 一、技术面评分（25 分）
# ============================================================
# 分值结构（2026-09-22 起）：趋势线 10 分 + 其余四项等比压缩 8/8/5/4 → 5/5/3/2（合计 15）
MA_TREND_FULL = 10      # 趋势线满分：MA5 > MA120 且 现价 ≥ MA120
MA_TREND_PASS = 6       # 趋势线及格：MA5 > MA20（未达满分）
MA_TREND_SCALE = 0.6    # 启用趋势线时，其余四项的分值缩放系数


def _pt(base: float, k: float) -> int:
    """按缩放系数折算分值（k=1.0 时即原分值 → 保证与改版前逐位一致）"""
    return int(round(base * k))


def _ma_trend_tier(row: pd.Series) -> Optional[str]:
    """趋势线档位（锚定 MA120）。

    'full' 满分：MA5 > MA120 且 现价 ≥ MA120
    'pass' 及格：MA5 > MA20（未达满分）
    'none' 0 分：两者都不满足
    None   数据缺失（无 ma 列 / NaN / MA120≤0，如次新股不足 120 根）
           → 本项不计分，其余四项按原口径给分（与改版前一致）
    """
    vals = []
    for key in ('ma5', 'ma20', 'ma120', 'price'):
        v = row.get(key, None)
        if v is None:
            return None
        try:
            if pd.isna(v):
                return None
            vals.append(float(v))
        except (TypeError, ValueError):
            return None
    ma5, ma20, ma120, px = vals
    if ma120 <= 0:
        return None
    if ma5 > ma120 and px >= ma120:
        return 'full'
    if ma5 > ma20:
        return 'pass'
    return 'none'


def calc_tech_score(row: pd.Series) -> Tuple[float, Dict]:
    """
    技术面评分（满分 25）

    - 当日涨幅（温和上涨加分，过热/深跌扣分）
    - 5日累计涨幅（趋势强度）
    - 60日涨幅（中长期趋势）
    - 量比（量能配合）
    - 【2026-09-22 新增】趋势线（锚定 MA120）
        满分 10：MA5 > MA120 且 现价 ≥ MA120
        及格  6：MA5 > MA20（未达满分）
        0     ：两者都不满足
        数据缺失：本项不计分，其余四项回到原 8/8/5/4 口径

    ⚠️ 前四项的**判据与阈值一行未改**，只把分值上限按 MA_TREND_SCALE 等比压缩；
       趋势线未启用（数据缺失 / MA120_TREND=off）时，输出与改版前逐位一致。
    """
    score = 0
    detail = {}

    tier = _ma_trend_tier(row)
    k = MA_TREND_SCALE if tier is not None else 1.0

    # 1) 当日涨幅（原 0~8 分）
    pct = row.get('pct_change', 0)
    if pd.isna(pct):
        pct = 0
    if 0 <= pct <= 3:
        score += _pt(8, k); detail['当日涨幅'] = f'{pct:.1f}% ({_pt(8, k)}分)'
    elif 3 < pct <= 6:
        score += _pt(6, k); detail['当日涨幅'] = f'{pct:.1f}% ({_pt(6, k)}分)'
    elif 6 < pct <= 9.5:
        score += _pt(4, k); detail['当日涨幅'] = f'{pct:.1f}% ({_pt(4, k)}分)'
    elif pct > 9.5:  # 涨停
        score += _pt(2, k); detail['当日涨幅'] = f'{pct:.1f}% 涨停 ({_pt(2, k)}分)'
    elif -2 <= pct < 0:
        score += _pt(4, k); detail['当日涨幅'] = f'{pct:.1f}% 微调 ({_pt(4, k)}分)'
    else:
        score += _pt(1, k); detail['当日涨幅'] = f'{pct:.1f}% 下跌 ({_pt(1, k)}分)'

    # 2) 5日累计涨幅（原 0~8 分）
    pct_5d = row.get('pct_5d', row.get('pct_change', 0))  # 没 5日数据时用当日
    if pd.isna(pct_5d):
        pct_5d = 0
    if 3 <= pct_5d <= 15:
        score += _pt(8, k); detail['5日涨幅'] = f'{pct_5d:.1f}% ({_pt(8, k)}分)'
    elif 15 < pct_5d <= 30:
        score += _pt(5, k); detail['5日涨幅'] = f'{pct_5d:.1f}% ({_pt(5, k)}分)'
    elif pct_5d > 30:
        score += _pt(2, k); detail['5日涨幅'] = f'{pct_5d:.1f}% 过热 ({_pt(2, k)}分)'
    else:
        score += _pt(3, k); detail['5日涨幅'] = f'{pct_5d:.1f}% ({_pt(3, k)}分)'

    # 3) 中长期趋势 - 60日涨幅（原 0~5 分）
    pct_60d = row.get('pct_60d', 0)
    if pd.isna(pct_60d):
        pct_60d = 0
    if pct_60d > 10:
        score += _pt(5, k); detail['60日趋势'] = f'{pct_60d:.1f}% ({_pt(5, k)}分)'
    elif pct_60d > 0:
        score += _pt(3, k); detail['60日趋势'] = f'{pct_60d:.1f}% ({_pt(3, k)}分)'
    elif pct_60d > -10:
        score += _pt(1, k); detail['60日趋势'] = f'{pct_60d:.1f}% ({_pt(1, k)}分)'
    else:
        score += _pt(0, k); detail['60日趋势'] = f'{pct_60d:.1f}% (0分)'

    # 4) 量比（原 0~4 分）
    vr = row.get('volume_ratio', 0)
    if pd.isna(vr):
        vr = 0
    if 1.5 <= vr <= 4:
        score += _pt(4, k); detail['量比'] = f'{vr:.2f} ({_pt(4, k)}分)'
    elif 1 <= vr < 1.5:
        score += _pt(2, k); detail['量比'] = f'{vr:.2f} ({_pt(2, k)}分)'
    elif vr > 4:
        score += _pt(2, k); detail['量比'] = f'{vr:.2f} 巨量 ({_pt(2, k)}分)'
    else:
        score += _pt(0, k); detail['量比'] = f'{vr:.2f} (0分)'

    # 5) 趋势线（锚定 MA120）—— 新增项
    if tier == 'full':
        score += MA_TREND_FULL
        detail['趋势线'] = f'MA5>年线 且价≥年线 ({MA_TREND_FULL}分)'
    elif tier == 'pass':
        score += MA_TREND_PASS
        detail['趋势线'] = f'MA5>MA20 ({MA_TREND_PASS}分)'
    elif tier == 'none':
        detail['趋势线'] = 'MA5≤MA20 (0分)'
    else:
        detail['趋势线'] = '数据不足·不计分'

    return min(score, SCORE_WEIGHTS['technical']), detail


# ============================================================
# 二、资金面评分（20 分）
# ============================================================
def _yi(wan) -> str:
    """万元 → 亿元字符串，并消除「-0.00」（负零在报告里会被误读为净流出）"""
    y = round(float(wan or 0.0) / 1e4, 2)
    if y == 0:
        y = 0.0
    return f'{y:+.2f}亿'


def _num(row: pd.Series, key: str):
    """安全取数：缺失/NaN/非数 → None（区别于 0，用于「无信号」判定）"""
    if key not in row:
        return None
    v = row.get(key)
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def capital_data_ok(row: pd.Series) -> bool:
    """该股近 5 日资金面数据是否足够判定（≥ CAPITAL_MIN_VALID_DAYS 天）。

    ⚠️ 门槛存在的理由（实测）：停牌/次新股近 5 日只有 2 天数据时，占比会被算成
    76% 这种虚高值（例：600825 仅 2 天数据 → 排到占比第一）。这类样本属于
    **数据质量假信号**，一律按「无信号」处理。
    """
    d = _num(row, 'elg_days_5d')
    return d is not None and d >= CAPITAL_MIN_VALID_DAYS


def is_capital_trend_ok(row: pd.Series) -> bool:
    """资金面趋势**硬门槛**：近 5 日日均超大单净额 > 近 60 日日均 且 > 0。

    实测支撑（主板 3168 只 × 7 个信号日）：不过滤的超额 −5.42%（原口径）；
    过滤后 −0.87%；通过率约 34% → 候选池仍 ~1000 只，不会卡死。

    ★ 以下两种情况返回 True（**不拦截**）：
      ① 面板整体不可用（列缺失）→ 降级，与均线面板「取不到就退回原口径」同哲学；
      ② 该股近 5 日有效数据不足 → **无法判定**（次新/停牌），不据此剔除，
         但资金面会自然给 0 分（用户拍板：数据不足即 0 分，而非排除）。
    """
    if 'elg_5d_avg' not in row or 'elg_60d_avg' not in row:
        return True                                   # ① 降级：不拦截
    if not capital_data_ok(row):
        return True                                   # ② 无法判定：不拦截
    e5 = _num(row, 'elg_5d_avg')
    e60 = _num(row, 'elg_60d_avg')
    if e5 is None or e60 is None:
        return True
    return e5 > e60 and e5 > 0


def calc_capital_score(row: pd.Series) -> Tuple[float, Dict]:
    """资金面 20 分 = 超大单趋势 10 + 超大单占比 4 + 换手率合理性 6（CAPITAL_MODE=elg）

    ★ 改造背景（2026-09-22）：原「当日主力净流入」前 5% 在主板 3168 只 × 7 个信号日上
      **7/7 全负超额**（平均 −5.42%），是典型的「当日放量大涨 → 追高」；而「净额/成交额
      占比」排序 −1.61% ≫「绝对净额」排序 −4.54%，故改用占比而非绝对额。
    ⚠️ 占比项只有 4 分（非主导）：实测「占比前5%」(−1.61%) 并不优于「过滤后全部」
      (−0.87%)，即占比**用于剔除有害项有余、用于选出赢家不足** → 保留话语权但不当主导。
    `CAPITAL_MODE=legacy` 时逐行回到旧口径（当日主力净流入 0~14 + 换手 0~6）。
    """
    if CAPITAL_MODE != 'elg':
        return _calc_capital_score_legacy(row)

    score = 0
    detail = {}

    # 1) 超大单趋势确认（0~10 分）
    if not capital_data_ok(row):
        # ★ 数据不足（次新/停牌）：视为「无信号」。
        #   默认（CAPITAL_MISSING_ZERO_SCOPE='all'）整个资金面 20 分归零 —— 即用户拍板的
        #   「资金面给 0 分」字面口径；置为 'elg_only' 时只归零超大单两项、换手率照常。
        detail = {'超大单趋势': f'数据不足（<{CAPITAL_MIN_VALID_DAYS}日）(0分)',
                  '超大单占比': '数据不足 (0分)'}
        if CAPITAL_MISSING_ZERO_SCOPE == 'all':
            detail['换手率'] = '数据不足 · 资金面整体归零 (0分)'
            return 0, detail
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
    else:
        e5 = _num(row, 'elg_5d_avg')
        e60 = _num(row, 'elg_60d_avg')
        if e5 is None or e60 is None:
            score += CAPITAL_ELG_NONE
            detail['超大单趋势'] = '缺失 (0分)'
        elif e5 > e60 and e5 > 0:
            score += CAPITAL_ELG_FULL
            detail['超大单趋势'] = f'5日日均{_yi(e5)} > 60日{_yi(e60)} (10分)'
        elif e5 > 0:
            score += CAPITAL_ELG_PARTIAL
            detail['超大单趋势'] = f'5日日均{_yi(e5)} 未放大 (5分)'
        else:
            score += CAPITAL_ELG_NONE
            detail['超大单趋势'] = f'5日日均{_yi(e5)} 净流出 (0分)'

        # 2) 超大单占比强度（0~4 分）：近 5 日净额 / 近 5 日成交额
        ratio = _num(row, 'elg_ratio_5d')
        if ratio is None:
            detail['超大单占比'] = '缺失 (0分)'
        else:
            got = 0
            for lo, pts in CAPITAL_RATIO_BANDS:
                if ratio > lo:
                    got = pts
                    break
            score += got
            detail['超大单占比'] = f'{ratio:+.2f}% ({got}分)'

    # 3) 换手率（0~6 分）—— 沿用原口径，一行未改
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


def _calc_capital_score_legacy(row: pd.Series) -> Tuple[float, Dict]:
    """旧口径（CAPITAL_MODE=legacy）：主力资金净流入 + 换手率合理性。

    ⚠️ 本函数是改造前的**逐行原样保留**，仅供回退对照使用 —— 改动前请勿修改。
    """
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
# 热门主题关键词（覆盖 A 股大部分常见板块，按子行业细分）
HOT_THEMES = {
    '农林牧渔': ['牧原', '温氏', '新希望', '海大', '大北农', '金新农', '正邦', '天邦', '益生', '民和', '圣农', '仙坛', '京基智农', '天康', '神农', '傲农', '华统', '唐人神', '立华', '益生股份', '圣农发展', '湘佳', '晓鸣', '巨星农牧', '普莱柯', '生物股份', '中牧', '瑞普'],
    '人工智能': ['科大讯飞', '中科曙光', '浪潮', '拓尔思', '汉王', '海康', '大华', '云从', '寒武纪', '商汤', '同方', '紫光', '海光', '景嘉微', '虹软', '中科创达', '依米康', '神州数码', '神州', '拓维', '博彦'],
    '游戏·互联网': ['吉比特', '三七', '恺英', '完美世界', '巨人', '游族', '昆仑万维', '神州泰岳', '世纪华通', '冰川网络', '电魂', '宝通', '富春', '姚记', '顺网', '盛天', '中青宝', '汤姆猫', '中文在线', '掌趣', '游久', '三五互联', '二三四五', '网宿', '数据港', '光环', '宝信', '芒果', '爱奇艺', '光线', '万达', '横店', '中国电影'],
    '半导体': ['中芯', '韦尔', '卓胜', '兆易', '长电', '通富', '华天', '北方华创', '中微', '沪硅', '雅克', '彤程', '晶瑞', '江丰', '士兰微', '华润微', '紫光国微', '闻泰', '三安', '立讯', '歌尔', '蓝思', '领益', '欧菲', '深南', '兴森', '景旺', '生益', '南亚', '华正', '金安国纪', '明阳', '新易盛', '中际', '光迅', '天孚'],
    '新能源车': ['比亚迪', '宁德', '亿纬', '赣锋', '天齐', '华友', '当升', '容百', '星源', '恩捷', '先导', '璞泰来', '杉杉', '长城', '长安', '广汽', '上汽', '蔚来', '理想', '小鹏', '江淮', '海马', '力帆', '众泰', '宇通', '金龙', '东风', '北汽蓝谷', '天赐', '多氟多', '永太', '天际', '国轩', '欣旺达', '鹏辉', '中伟', '寒锐', '盛新', '厦门钨业', '金力永磁', '北方稀土'],
    '光伏': ['隆基', '通威', '阳光电源', '金风', '明阳', '天合', '晶澳', '晶科', '福莱特', '福莱特', '福斯特', '旗滨', '南玻', '亚玛顿', '东方日升', '中来', '爱旭', '上机数控', '迈为', '捷佳', '金辰', '帝尔', '山煤', '金博', '美畅', '高测', '双良', '京运通', '太阳能', '节能'],
    '军工': ['中航', '航发', '中船', '中国船舶', '中国重工', '中国动力', '洪都', '西飞', '沈飞', '哈飞', '成飞', '航发动力', '航发控制', '中航电子', '中航沈飞', '中航高科', '中航重机', '中航光电', '中航资本', '中航产融', '中国卫星', '航天电子', '航天科技', '航天彩虹', '航天长峰', '航天信息', '航天电器', '北方导航', '北方国际', '中国一重', '中国核建', '中国核电', '中核科技', '中国核工业'],
    '医药': ['恒瑞', '药明', '智飞', '迈瑞', '爱尔', '通策', '片仔癀', '云南白药', '同仁堂', '东阿', '复星', '华东医药', '丽珠', '健康元', '科伦', '华润', '上海医药', '白云山', '复星医药', '康龙化成', '泰格', '凯莱英', '博腾', '昭衍', '药石', '百济', '信达', '君实', '复宏汉霖', '甘李', '通化东宝', '长春高新', '我武', '安科'],
    '白酒·食品': ['茅台', '五粮液', '泸州老窖', '洋河', '汾酒', '古井贡', '今世缘', '口子窖', '迎驾贡', '舍得', '酒鬼酒', '水井坊', '伊利', '蒙牛', '光明', '三元', '双汇', '雨润', '得利斯', '龙大', '海天', '中炬', '加加', '恒顺', '涪陵', '安井', '三全', '思念', '海欣', '桃李', '达利', '盼盼', '亲亲', '好想你', '盐津铺子', '洽洽', '来伊份', '良品铺子', '绝味', '煌上煌', '周黑鸭', '百润'],
    '银行·金融': ['招商银行', '兴业银行', '浦发银行', '民生银行', '中信银行', '光大银行', '华夏银行', '平安银行', '宁波银行', '南京银行', '杭州银行', '江苏银行', '上海银行', '长沙银行', '成都银行', '重庆银行', '郑州银行', '青岛银行', '齐鲁银行', '苏州银行', '青农商行', '渝农商行', '紫金银行', '中国平安', '新华保险', '中国太保', '中国人寿', '中国人保', '中国太平', '国元证券', '东北证券', '长江证券', '西部证券', '国海证券', '山西证券', '国金证券', '中泰证券', '中信建投', '中信证券', '海通证券', '华泰证券', '国泰君安', '招商证券', '广发证券', '申万宏源', '东方财富', '同花顺', '大智慧'],
    '地产·基建': ['万科', '保利', '金地', '招商蛇口', '华夏幸福', '金科', '中南', '阳光城', '新城', '荣盛', '滨江', '建发', '首开', '城建', '中交', '中国建筑', '中国铁建', '中国中铁', '中国电建', '中国能建', '中国交建', '中国化学', '中国中冶', '中国一重', '葛洲坝', '安徽建工', '山东路桥', '四川路桥', '新疆交建', '隧道股份', '上海建工', '龙建股份', '粤水电', '宏润建设'],
    '传媒·影视': ['分众', '新潮', '蓝色光标', '利欧', '引力', '省广', '华扬联众', '三人行', '宣亚', '视觉中国', '新华网', '人民网', '浙文互联', '北巴', '龙韵', '幸福蓝海', '华谊兄弟', '光线', '北京文化', '中国电影', '万达', '横店', '上海电影', '中视传媒'],
    '汽车·零部件': ['上汽', '广汽', '长安', '长城', '吉利', '奇瑞', '比亚迪', '江淮', '海马', '力帆', '众泰', '宇通', '金龙', '东风', '北汽蓝谷', '华域汽车', '福耀玻璃', '星宇', '伯特利', '拓普', '银轮', '三花', '盾安', '凌云', '岱美', '继峰', '富奥', '宁波华翔', '均胜', '德赛', '华阳', '索菱', '路畅', '德赛西威'],
    '家电': ['美的', '格力', '海尔', '海信', 'TCL', '长虹', '康佳', '创维', '老板', '华帝', '万和', '苏泊尔', '九阳', '爱仕达', '小熊', '北鼎', '新宝', '莱克', '科沃斯', '石头', '云鲸', '极米', '光峰'],
    '化工·材料': ['万华', '恒力', '荣盛', '桐昆', '新凤鸣', '恒逸', '东方盛虹', '恒力石化', '桐昆股份', '宝丰', '卫星', '华鲁恒升', '鲁西', '阳煤', '兖矿', '兰花', '昊华', '利尔', '蓝丰', '红太阳', '扬农', '新安', '江山', '联化', '雅本', '利安隆', '利民', '长青', '广信', '丰山', '先达'],
    '钢铁·有色': ['宝钢', '武钢', '河钢', '鞍钢', '首钢', '太钢', '马钢', '本钢', '华菱', '三钢', '方大', '南钢', '杭钢', '山东钢铁', '酒钢', '西宁特钢', '抚顺特钢', '永兴', '久立', '武进不锈', '永和', '金洲管道', '新兴铸管', '金岭', '中金岭南', '云铝', '南山铝业', '明泰', '银邦', '楚江', '金田铜业', '海亮', '金发', '铜陵', '江西铜业', '云南铜业', '中金黄金', '山东黄金', '紫金矿业', '湖南黄金', '恒邦', '赤峰黄金', '银泰黄金', '招金矿业'],
    '通信·5G': ['中兴', '烽火', '光迅', '中际', '新易盛', '天孚', '剑桥', '博创', '太辰光', '科信', '亨通', '中天', '长飞', '通鼎', '特发', '亿联', '会畅', '视源', '亿联网络', '二六三', '梦网', '吴通', '海格', '海能达', '海能', '七一二', '上海瀚讯'],
    '能源·煤炭': ['中煤', '中国神华', '陕西煤业', '兖矿能源', '淮北矿业', '山西焦煤', '平煤', '潞安', '晋能', '兰花', '山煤国际', '昊华', '中泰化学', '宝丰能源', '卫星化学', '中国石化', '中国石油', '中海油', '广汇能源', '新奥', '蓝焰', '新天然气', '百川', '陕西黑猫'],
    '航运·港口': ['招商南油', '招商轮船', '中远海控', '中远海发', '中远海能', '中远海特', '中国船舶', '中国重工', '中国动力', '招商港口', '广州港', '宁波港', '唐山港', '天津港', '上港集团', '厦门港务', '盐田港', '北部湾港', '秦港股份', '连云港', '辽港股份', '南京港', '重庆港九', '锦州港'],
    '消费·零售': ['永辉', '家家悦', '红旗', '中百', '步步高', '王府井', '百联', '重庆百货', '欧亚', '银座', '翠微', '三江购物', '联华', '华联', '大商', '首商', '友阿', '徐家汇', '上海九百', '杭州解百', '海宁皮城', '新华都', '人人乐', '华润万家', '屈臣氏', '永辉超市', '贵州茅台'],
    '教育·人服': ['中公', '新东方', '好未来', '高途', '网易有道', '粉笔', '思考乐', '豆神', '佳发', '拓维信息', '威创', '秀强', '和晶', '全通', '立思辰', '豆神教育'],
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

    ⚠️ 与改版前的唯一差异 = **资金面趋势硬门槛**（`CAPITAL_MODE=elg` 时启用）：
       不满足「近5日日均超大单净额 > 近60日日均 且 >0」的票**直接从候选池剔除**，
       不占 TOP5 名额（与实证口径一致）。取不到面板数据时自动跳过该门槛。
    """
    if df.empty:
        return df, df, df

    print(f'\n🔍 开始评分 {len(df)} 只候选股票...')

    # ★ 资金面趋势硬门槛（因子条件，非风险条件；被剔除的票不进警示名单）
    if CAPITAL_MODE == 'elg' and CAPITAL_TREND_FILTER and 'elg_5d_avg' in df.columns:
        n_before = len(df)
        _keep = df[df.apply(is_capital_trend_ok, axis=1)]
        if _keep.empty:
            print('  ⚠️ 资金面趋势门槛后无候选 → 视为数据异常，**本次不启用该门槛**（避免空跑）')
        else:
            df = _keep
            print(f'  🚦 资金面趋势门槛（近5日日均 > 近60日日均 且 >0）：'
                  f'{n_before} → {len(df)} 只（剔除 {n_before - len(df)} 只）')

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
# 盘后复盘：板块温度分析
# ============================================================
def tag_theme(name: str) -> str:
    """按股票名称匹配热门题材，未匹配返回『其他』"""
    name = str(name)
    for theme, keywords in HOT_THEMES.items():
        for kw in keywords:
            if kw in name:
                return theme
    return '其他'


def analyze_sector_heat(df: pd.DataFrame, top_n: int = 3) -> List[Dict]:
    """
    基于当日全市场数据，按题材聚类计算板块温度，返回 TOP 板块。
    每个板块统计：
    - avg_pct: 平均涨幅（%）
    - inflow_yi: 主力净流入合计（亿）
    - limit_up: 涨停家数（pct_chg >= 9.8）
    - cnt: 板块内股票数
    - strength: 强度分 = 平均涨幅*0.4 + max(主力流入,0)*0.3 + 涨停数*0.3

    返回 [{'theme','avg_pct','inflow_yi','limit_up','cnt','strength'}, ...] 按 strength 降序
    """
    if df is None or df.empty:
        return []

    df = df.copy()
    if 'code' in df.columns:
        df['code'] = df['code'].astype(str).str.zfill(6)
    # 打题材标签
    df['theme'] = df['name'].astype(str).apply(tag_theme)
    # 排除未匹配题材的股票
    df = df[df['theme'] != '其他']
    if df.empty:
        return []

    # 确保数值列可用
    # ⚠️ 与 analyze_sector_leaders 同样的列缺失陷阱：`df.get('pct_change', 0)` 返回标量 0 →
    #    .fillna() 抛 AttributeError。此处补列存在判断（纯健壮性，正常链路行为不变）。
    if 'pct_change' not in df.columns:
        df['pct_change'] = 0.0
    df['pct_change'] = pd.to_numeric(df['pct_change'], errors='coerce').fillna(0)
    if 'main_net_inflow' not in df.columns:
        df['main_net_inflow'] = 0.0
    df['main_net_inflow'] = pd.to_numeric(df['main_net_inflow'], errors='coerce').fillna(0)

    result = []
    for theme, g in df.groupby('theme'):
        avg_pct = float(g['pct_change'].mean())
        inflow_yi = float(g['main_net_inflow'].sum()) / 1e8
        limit_up = int((g['pct_change'] >= 9.8).sum())
        cnt = len(g)
        # 强度分（加权，capture 板块整体热度）
        strength = avg_pct * 0.4 + max(inflow_yi, 0) * 0.3 + limit_up * 0.3
        result.append({
            'theme': theme,
            'avg_pct': round(avg_pct, 2),
            'inflow_yi': round(inflow_yi, 1),
            'limit_up': limit_up,
            'cnt': cnt,
            'strength': round(strength, 2),
        })

    result.sort(key=lambda x: x['strength'], reverse=True)
    return result[:top_n]


def tag_sector(name: str, industry: str = '') -> str:
    """板块归类（两级匹配）。

    1) 优先热门题材关键词（HOT_THEMES，人工维护的概念题材）
    2) 未命中 → 回退 Tushare 行业分类（industry）
    3) 都拿不到 → 「其他」

    背景：原 tag_theme 只做第 1 级，实测全市场覆盖率仅约 13%（705/5550）。
    """
    t = tag_theme(str(name))
    if t != '其他':
        return t
    ind = str(industry).strip() if industry is not None else ''
    if ind and ind.lower() != 'nan' and ind != '其他':
        return ind
    return '其他'


def analyze_sector_leaders(df: pd.DataFrame, top_n: int = 3,
                           min_cnt: int = 3,
                           industry_map: Dict[str, str] = None) -> Dict[str, List[Dict]]:
    """把「板块温度」单一混合榜**拆成三个独立榜**（涨幅 / 资金 / 涨停）。

    背景（2026-09-17 实测，脚本 `_ts_sector_heat_probe.py`）：
      原 strength = 平均涨幅×0.4 + max(主力净流入,0)×0.3 + 涨停数×0.3，
      三项量纲分别是 %（摆动 3.8）/ 亿（摆动 124）/ 只（摆动 2），
      加权后实际摆动幅度 = 1.52 / 37.25 / 0.60 分
      → **资金项话语权是涨幅的 24.5 倍**，实测 TOP1 半导体 96.3% 贡献来自资金，
        「板块温度」名不副实（实为资金榜）。
      → 本函数不再做混合加权，改为**三榜独立排序**：口径透明、无参数争议、
        也不会出现「下跌板块排在上涨板块前面」的困惑。

    参数：
      df           需含 name / pct_change / main_net_inflow，可选 code / industry
      top_n        每榜返回条数
      min_cnt      板块成分股下限（行业兜底后会混入只有 1~2 只的细行业 → 噪声）
      industry_map 6 位代码 → 行业；缺省时自动获取（失败则退化为纯题材匹配）

    返回 {'gainers': [...], 'flows': [...], 'limit_ups': [...], 'all': [...], 'coverage': {...}}
    """
    if df is None or df.empty:
        return {'gainers': [], 'flows': [], 'limit_ups': [], 'all': [], 'coverage': {}}

    d = df.copy()
    if 'name' not in d.columns:
        d['name'] = ''
    if 'code' in d.columns:
        codes = d['code'].astype(str).str.zfill(6)
    else:
        codes = pd.Series([''] * len(d), index=d.index)

    if industry_map is None:
        try:
            from .data_fetcher import fetch_industry_map
            industry_map = fetch_industry_map() or {}
        except Exception:
            industry_map = {}

    if 'industry' in d.columns:
        inds = [str(x) for x in d['industry'].tolist()]
    else:
        inds = [industry_map.get(c, '') for c in codes]
    d['_sector'] = [tag_sector(n, i) for n, i in zip(d['name'].astype(str), inds)]

    # ⚠️ 必须先判列存在再取值：`d.get('col', 0)` 在列缺失时返回**标量 0**（不是 Series），
    #    紧接着的 .fillna() 会抛 AttributeError: 'int' object has no attribute 'fillna'。
    #    （与项目里 `row.get(k,0)` 返回 None 的陷阱同类）
    if 'pct_change' not in d.columns:
        d['pct_change'] = 0.0
    d['pct_change'] = pd.to_numeric(d['pct_change'], errors='coerce').fillna(0.0)
    if 'main_net_inflow' not in d.columns:
        d['main_net_inflow'] = 0.0
    d['main_net_inflow'] = pd.to_numeric(d['main_net_inflow'], errors='coerce').fillna(0.0)
    # ⚠️ **拍板决策（2026-09-23 用户明确）**：板块「资金榜」**保持当日主力净额口径，不变**。
    #    背景：本轮个股资金面因子已改为「超大单 5日/60日 趋势 + 占比」（`CAPITAL_MODE='elg'`），
    #    两者口径不再一致。已就此提问，用户选择「板块资金榜仍用当日净额逻辑不变」
    #    ⇒ **不要再把此处改成超大单口径**（不是遗漏，是有意为之）。

    total = len(d)
    tagged = int((d['_sector'] != '其他').sum())

    rows = []
    for sector, g in d.groupby('_sector'):
        if sector == '其他':
            continue
        rows.append({
            'theme': sector,
            'avg_pct': float(g['pct_change'].mean()),
            'inflow_yi': float(g['main_net_inflow'].sum()) / 1e8,
            'limit_up': int((g['pct_change'] >= 9.8).sum()),
            'cnt': len(g),
        })
    if not rows:
        # ⚠️ 该分支必须与下方成功分支的 coverage 字段**完全一致**，否则「一只都没归类上」
        #    这种最该提示降级的情况下，报告侧反而拿不到 industry_downgraded → 静默无提示。
        return {'gainers': [], 'flows': [], 'limit_ups': [], 'all': [],
                'coverage': {'total': total, 'tagged': tagged, 'ratio': 0.0,
                             'n_sector': 0, 'min_cnt': min_cnt,
                             'industry_downgraded': not bool(industry_map)}}

    t = pd.DataFrame(rows)
    t = t[t['cnt'] >= max(1, int(min_cnt))]
    if t.empty:                       # 门槛过严 → 退回不过滤
        t = pd.DataFrame(rows)

    def _pack(frame):
        out = []
        for _, r in frame.iterrows():
            out.append({
                'theme': r['theme'],
                'avg_pct': round(float(r['avg_pct']), 2),
                'inflow_yi': round(float(r['inflow_yi']), 1),
                'limit_up': int(r['limit_up']),
                'cnt': int(r['cnt']),
            })
        return out

    return {
        'gainers': _pack(t.sort_values(['avg_pct', 'cnt'], ascending=[False, False]).head(top_n)),
        'flows': _pack(t.sort_values(['inflow_yi', 'cnt'], ascending=[False, False]).head(top_n)),
        'limit_ups': _pack(t.sort_values(['limit_up', 'avg_pct'], ascending=[False, False]).head(top_n)),
        'all': _pack(t.sort_values('avg_pct', ascending=False)),
        'coverage': {
            'total': total,
            'tagged': tagged,
            'ratio': round(tagged * 100.0 / total, 1) if total else 0.0,
            'n_sector': int(len(t)),
            'min_cnt': int(min_cnt),
            'industry_downgraded': not bool(industry_map),
        },
    }


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