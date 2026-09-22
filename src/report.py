"""
报告生成模块 - 输出 Markdown 报告 + 钉钉推送内容
==================================================
- 钉钉机器人 markdown 子集支持：标题、加粗、引用、列表、表格、emoji
- 不支持 :::tip 等扩展语法
"""
import json
from datetime import datetime
from typing import Dict, List, Tuple, Any, Optional
import pandas as pd
from pathlib import Path

# 持仓规则常量统一从 portfolio 模块取（单一来源，防止两处默认值走偏）
try:
    from src.portfolio import (
        MAX_HOLDINGS as _MAX_HOLDINGS,
        DEFAULT_STOP_LOSS_PCT as _STOP_LOSS_PCT,
        EXIT_USE_STOP as _EXIT_USE_STOP,
        EXIT_USE_MA as _EXIT_USE_MA,
        EXIT_MA_HALF as _EXIT_MA_HALF,
        EXIT_MA_CLEAR as _EXIT_MA_CLEAR,
        TRAIL_ENABLED as _TRAIL_ENABLED,
        TRAIL_ACTIVATE_PCT as _TRAIL_ACTIVATE_PCT,
        TRAIL_DRAWDOWN_PCT as _TRAIL_DRAWDOWN_PCT,
        TIME_STOP_ENABLED as _TIME_STOP_ENABLED,
        ZOMBIE_DAYS as _ZOMBIE_DAYS,
        ZOMBIE_PEAK_PCT as _ZOMBIE_PEAK_PCT,
        INEFFICIENT_DAYS as _INEFFICIENT_DAYS,
        INEFFICIENT_PEAK_PCT as _INEFFICIENT_PEAK_PCT,
        MARKET_FUSE_ENABLED as _MARKET_FUSE_ENABLED,
        MARKET_FUSE_INDEX as _MARKET_FUSE_INDEX,
        MARKET_FUSE_DROP_PCT as _MARKET_FUSE_DROP_PCT,
        MARKET_FUSE_BLOCK_REFILL as _MARKET_FUSE_BLOCK_REFILL,
    )
except ImportError:  # 兼容以顶层模块方式导入
    from portfolio import (
        MAX_HOLDINGS as _MAX_HOLDINGS,
        DEFAULT_STOP_LOSS_PCT as _STOP_LOSS_PCT,
        EXIT_USE_STOP as _EXIT_USE_STOP,
        EXIT_USE_MA as _EXIT_USE_MA,
        EXIT_MA_HALF as _EXIT_MA_HALF,
        EXIT_MA_CLEAR as _EXIT_MA_CLEAR,
        TRAIL_ENABLED as _TRAIL_ENABLED,
        TRAIL_ACTIVATE_PCT as _TRAIL_ACTIVATE_PCT,
        TRAIL_DRAWDOWN_PCT as _TRAIL_DRAWDOWN_PCT,
        TIME_STOP_ENABLED as _TIME_STOP_ENABLED,
        ZOMBIE_DAYS as _ZOMBIE_DAYS,
        ZOMBIE_PEAK_PCT as _ZOMBIE_PEAK_PCT,
        INEFFICIENT_DAYS as _INEFFICIENT_DAYS,
        INEFFICIENT_PEAK_PCT as _INEFFICIENT_PEAK_PCT,
        MARKET_FUSE_ENABLED as _MARKET_FUSE_ENABLED,
        MARKET_FUSE_INDEX as _MARKET_FUSE_INDEX,
        MARKET_FUSE_DROP_PCT as _MARKET_FUSE_DROP_PCT,
        MARKET_FUSE_BLOCK_REFILL as _MARKET_FUSE_BLOCK_REFILL,
    )

# 资金面口径（'elg' = 超大单 5日/60日 趋势；'legacy' = 当日主力净流入）
try:
    from src.config import CAPITAL_MODE, CAPITAL_DAY_RATIO_CAP
except ImportError:      # 兼容以顶层模块方式导入
    from config import CAPITAL_MODE, CAPITAL_DAY_RATIO_CAP


# TOP 表第 7 列列名（新口径 = 超大单占比 / legacy = 当日主力净额）—— 模块级，
# 早上选股与盘后复盘两张表共用，避免函数内局部变量跨函数引用
ELG_HEAD = '超大单占比' if CAPITAL_MODE == 'elg' else '主力净额'


def screen_footnote() -> str:
    """筛选口径脚注（随 CAPITAL_MODE 变化，保证回退路径文案不变）

    单日占比上限启用时追加说明（用户拍板 2026-09-23）——
    该约束会**改变超大单数值本身**（截顶后），不写出来会让人误以为数据被低估。
    """
    if CAPITAL_MODE == 'elg':
        _base = '> 🎯 筛选：主板非ST / 趋势向上 / 超大单5日日均>60日日均'
        if CAPITAL_DAY_RATIO_CAP and CAPITAL_DAY_RATIO_CAP > 0:
            _base += f'（单日占比已截顶 {CAPITAL_DAY_RATIO_CAP:g}%）'
        return _base
    return '> 🎯 筛选：主板非ST / 趋势向上 / 主力流入'


def format_price(price: float) -> str:
    if pd.isna(price):
        return '-'
    return f'{price:.2f}'


def format_pct(pct: float) -> str:
    if pd.isna(pct):
        return '-'
    if pct >= 0:
        return f'+{pct:.2f}%'
    return f'{pct:.2f}%'


def pct_color(pct) -> str:
    """A 股惯例配色：涨 / 正值 = 🔴，跌 / 负值 = 🟢（**与国际市场相反，切勿写反**）。

    用于「板块涨幅」「主力净额」等带方向的数值，保证全报告配色统一。
    取数失败（None / NaN / 非数字）→ ⚪，不误染红绿。
    """
    try:
        v = float(pct)
    except (TypeError, ValueError):
        return '⚪'
    if pd.isna(v):
        return '⚪'
    return '🔴' if v >= 0 else '🟢'


def format_yi(amount: float) -> str:
    """把金额转为亿"""
    if pd.isna(amount) or amount == 0:
        return '-'
    yi = amount / 1e8
    if yi >= 0:
        return f'+{yi:.1f}亿'
    return f'{yi:.1f}亿'


def safe_float(value) -> Optional[float]:
    """None / NaN / 非数字 → None（避免 .2f 格式化崩溃）"""
    try:
        if value is None or pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def format_num(value, spec: str = '.2f', suffix: str = '') -> str:
    """None / NaN 安全的数字格式化，缺失时返回 '-'"""
    v = safe_float(value)
    if v is None:
        return '-'
    return f'{v:{spec}}{suffix}'


def format_mcap_yi(mcap) -> str:
    """流通市值（元）→ 亿，缺失返回 '-'"""
    v = safe_float(mcap)
    if v is None:
        return '-'
    return f'{v / 1e8:.1f}亿'


# ============================================================
# 资金面展示（2026-09-22 新口径：超大单 5日/60日 趋势 + 占比）
# ============================================================
def _yi(wan) -> str:
    """万元 → 亿元字符串，并消除「-0.00」（负零在报告里会被误读为净流出）"""
    y = round(float(wan or 0.0) / 1e4, 2)
    if y == 0:
        y = 0.0
    return f'{y:+.2f}亿'


def elg_cell(row) -> str:
    """TOP 表格「超大单占比」列：近 5 日超大单净额 / 近 5 日成交额（%）。

    `CAPITAL_MODE=legacy` → 退回原「主力净额」（亿），保证回退路径展示不变。
    """
    if CAPITAL_MODE != 'elg':
        return format_yi(row.get('main_net_inflow', 0))
    v = safe_float(row.get('elg_ratio_5d'))
    if v is None:
        return '-'
    return f'{v:+.2f}%'


def elg_cell_color(row) -> str:
    """「超大单占比」列的配色（A 股惯例：正=🔴 / 负=🟢）"""
    if CAPITAL_MODE != 'elg':
        return pct_color(row.get('main_net_inflow', 0))
    return pct_color(safe_float(row.get('elg_ratio_5d')))


def capital_short(row) -> str:
    """一句话描述资金面（「今日重点」用，尽量短）。"""
    if CAPITAL_MODE != 'elg':
        return f'主力 {format_yi(row.get("main_net_inflow", 0))}流入'
    v = safe_float(row.get('elg_5d_avg'))
    if v is None:
        return '资金面数据不足'
    r = safe_float(row.get('elg_ratio_5d'))
    ratio_txt = f'（占比{r:+.1f}%）' if r is not None else ''
    e60 = safe_float(row.get('elg_60d_avg'))
    if v > 0 and e60 is not None and v > e60:
        return f'超大单持续流入 {_yi(v)}/日{ratio_txt}'
    if v > 0:
        return f'超大单流入 {_yi(v)}/日{ratio_txt}'
    return f'⚠️超大单净流出 {_yi(v)}/日'


def capital_state(row) -> str:
    """资金面状态：'strong' / 'inflow' / 'outflow' / 'unknown'（按 CAPITAL_MODE 分派）。

    ⚠️ legacy 分支的阈值（strong ≥ +1 亿、outflow < −0.5 亿）与改造前**逐字一致**，
    保证 `CAPITAL_MODE=legacy` 回退时「总体策略建议」的结论与旧版完全相同。
    """
    if CAPITAL_MODE != 'elg':
        inflow_yi = (safe_float(row.get('main_net_inflow')) or 0.0) / 1e8
        if inflow_yi >= 1:
            return 'strong'
        if inflow_yi < -0.5:
            return 'outflow'
        return 'inflow' if inflow_yi > 0 else 'unknown'

    v = safe_float(row.get('elg_5d_avg'))
    if v is None:
        return 'unknown'
    if v <= 0:
        return 'outflow'
    e60 = safe_float(row.get('elg_60d_avg'))
    if e60 is not None and v > e60:
        return 'strong'
    return 'inflow'


def capital_detail_lines(row) -> List[str]:
    """个股明细里的资金面两行（新口径 = 超大单 5日日均 + 占成交额；legacy = 当日主力净额）。"""
    if CAPITAL_MODE != 'elg':
        return [f'- 主力净流入：{format_yi(row.get("main_net_inflow", 0))}']
    e5 = safe_float(row.get('elg_5d_avg'))
    e60 = safe_float(row.get('elg_60d_avg'))
    r = safe_float(row.get('elg_ratio_5d'))
    if e5 is None:
        return ['- 超大单 5日日均：数据不足（次新/停牌）']
    out = [f'- 超大单 5日日均：**{_yi(e5)}**' + (f'（60日 {_yi(e60)}）' if e60 is not None else '')]
    if r is not None:
        out.append(f'- 超大单占成交额：**{r:+.2f}%**')
    return out


def capital_tips(row) -> List[str]:
    """「操作指引」里的资金面子项（新口径：5日/60日趋势 + 占比强度）。"""
    if CAPITAL_MODE != 'elg':
        inflow_yi = (safe_float(row.get('main_net_inflow')) or 0.0) / 1e8
        if inflow_yi >= 3:
            return [f'主力强势介入(+{inflow_yi:.1f}亿)']
        if inflow_yi >= 1:
            return [f'主力净流入(+{inflow_yi:.1f}亿)']
        if inflow_yi > 0:
            return [f'主力温和流入(+{inflow_yi:.1f}亿)']
        if inflow_yi > -0.5:
            return [f'主力微流出({inflow_yi:.1f}亿)']
        return [f'⚠️主力撤离({inflow_yi:.1f}亿)']

    v = safe_float(row.get('elg_5d_avg'))
    if v is None:
        return ['资金面数据不足（次新/停牌）']
    e60 = safe_float(row.get('elg_60d_avg'))
    out = []
    if v > 0 and e60 is not None and v > e60:
        out.append(f'超大单持续流入(5日日均{_yi(v)} > 60日{_yi(e60)})')
    elif v > 0:
        out.append(f'超大单流入未放大(5日日均{_yi(v)})')
    else:
        out.append(f'⚠️超大单净流出(5日日均{_yi(v)})')
    r = safe_float(row.get('elg_ratio_5d'))
    if r is not None:
        out.append(f'超大单占成交额{r:+.2f}%')
    return out


def generate_keystrokes(df: pd.DataFrame) -> List[str]:
    """生成「今日重点」3-5 条极简要点"""
    if df.empty:
        return ['今日未筛到符合条件的标的，建议观望']

    points = []
    # 取 TOP 1
    top1 = df.iloc[0]
    points.append(
        f'🥇 龙头 {top1["name"]}({top1["code"]}) '
        f'{capital_short(top1)}，评分 {top1["total_score"]:.0f}'
    )

    # 涨停股
    limit_ups = df[df['pct_change'] >= 9.5]
    if len(limit_ups) > 0:
        names = '、'.join(limit_ups['name'].tolist()[:3])
        points.append(f'🚀 涨停股：{names}（次日分歧风险高）')

    # 板块联动（统计出现频率高的题材）
    return points


def generate_theme_heatmap(top_picks: pd.DataFrame, all_stocks: pd.DataFrame) -> List[str]:
    """
    生成「板块热度 TOP3」要点
    逻辑：
    - 从 TOP 5 中提取题材作为信号
    - 同时统计全主板的题材占比作为强度
    - TOP 5 中某题材占比 >= 30% 视为"板块联动信号"
    """
    if top_picks.empty:
        return []

    lines = []
    theme_count = {}  # 题材 → TOP 5 出现次数
    theme_score = {}  # 题材 → 平均评分

    for _, row in top_picks.iterrows():
        breakdown = row.get('score_breakdown', {})
        for k, v in breakdown.items():
            if k == '题材':
                theme_name = v.split(' ')[0]
                theme_count[theme_name] = theme_count.get(theme_name, 0) + 1
                theme_score.setdefault(theme_name, []).append(row.get('total_score', 0))
                break

    if not theme_count:
        return []

    # 按出现次数 + 平均评分排序，取 TOP 3
    def sort_key(item):
        theme, cnt = item
        avg_score = sum(theme_score[theme]) / len(theme_score[theme])
        # 权重：出现次数 60% + 平均评分 40%
        return (cnt * 100 / len(top_picks) * 0.6 + avg_score * 0.4, cnt, avg_score)

    sorted_themes = sorted(theme_count.items(), key=sort_key, reverse=True)[:3]

    for i, (theme, cnt) in enumerate(sorted_themes, 1):
        medal = ['🥇', '🥈', '🥉'][i - 1] if i <= 3 else f'{i}'
        avg_score = sum(theme_score[theme]) / len(theme_score[theme])
        pct = cnt * 100 / len(top_picks)
        signal = '🔥 强联动' if pct >= 60 else ('🔸 中联动' if pct >= 30 else '🔹 弱联动')
        lines.append(
            f'{medal} **{theme}** - {signal}，'
            f'TOP 5 占比 {pct:.0f}%，平均评分 {avg_score:.0f}'
        )

    return lines


def generate_action_tips(row: pd.Series) -> str:
    """
    基于股票的具体属性，生成针对性的操作建议（不再千篇一律）。
    根据以下特征优先级生成建议：
    1) 资金面（新口径：超大单 5日/60日 趋势 + 占比；legacy：主力净流入强度）
    2) 当日涨幅（避免追高）
    3) PE 估值水平
    4) 换手率（活跃度/风险）
    5) 60 日趋势稳定性
    """
    tips = []

    # 1) 资金面信号（新口径：超大单 5日/60日 趋势 + 占比；legacy 退回主力净流入）
    tips.extend(capital_tips(row))

    # 2) 当日涨幅
    pct = float(row.get('pct_change', 0) or 0)
    if pct >= 9.5:
        tips.append('已涨停，次日分歧风险高')
    elif pct >= 6:
        tips.append(f'今日+{pct:.1f}%已涨不少，等回调再关注')
    elif pct >= 3:
        tips.append(f'今日+{pct:.1f}%稳健上行')
    elif pct >= 0:
        tips.append(f'今日+{pct:.1f}%温和，可关注')
    elif pct >= -2:
        tips.append(f'今日{pct:.1f}%微调，可能低吸机会')
    else:
        tips.append(f'今日{pct:.1f}%下跌，需谨慎')

    # 3) PE 估值
    pe = float(row.get('pe_ttm', 0) or 0)
    if pe > 0 and pe < 15:
        tips.append(f'PE {pe:.0f}估值便宜')
    elif pe >= 15 and pe < 30:
        tips.append(f'PE {pe:.0f}估值合理')
    elif pe >= 30 and pe < 60:
        tips.append(f'PE {pe:.0f}估值偏高')
    elif pe >= 60:
        tips.append(f'PE {pe:.0f}高估需警惕')
    elif pe < 0:
        tips.append('⚠️业绩亏损')

    # 4) 换手率
    turnover = float(row.get('turnover_rate', 0) or 0)
    if turnover > 20:
        tips.append(f'换手{turnover:.1f}%过热')
    elif turnover < 1:
        tips.append(f'换手{turnover:.1f}%偏低')
    elif 3 <= turnover <= 10:
        tips.append(f'换手{turnover:.1f}%活跃健康')

    # 5) 总体策略建议（资金面按 CAPITAL_MODE 分派；legacy 阈值与旧版逐字一致）
    _cap = capital_state(row)
    if pct >= 9.5:
        conclusion = '不建议追高'
    elif _cap == 'strong' and 0 < pct < 6 and pe < 50:
        conclusion = '可小仓试探'
    elif _cap == 'outflow' or pct >= 6:
        conclusion = '观望为主'
    else:
        conclusion = '回调时分批关注'

    return '｜'.join(tips[:3]) + f'。{conclusion}。'


def generate_market_section(market: Dict[str, Any]) -> List[str]:
    """
    生成「大盘环境」markdown 段（上证指数 + 主力净额 + 60日趋势）
    返回纯 markdown 行列表（不含标题与分隔），由调用方拼到 payload 中
    """
    lines = []
    idx_close = market.get('index_close')
    idx_pct = market.get('index_pct_change')
    main_yi = market.get('main_net_inflow_yi')
    trend = market.get('trend_60d', 'unknown')
    trend_detail = market.get('trend_detail', '')

    # 上证指数一行
    if idx_close is not None:
        lines.append(f'- **上证指数**：{idx_close:.2f}（{format_pct(idx_pct)}）')
    else:
        lines.append('- **上证指数**：数据缺失')

    # 主力净额一行（A 股惯例：红＝净流入/正面，绿＝净流出）
    if main_yi is not None:
        emoji = pct_color(main_yi)
        lines.append(f'- **大盘主力**：{emoji} {main_yi:+.1f} 亿')
    else:
        lines.append('- **大盘主力**：数据缺失')

    # 60日趋势一行
    if trend != 'unknown' and trend_detail:
        lines.append(f'- **60日趋势**：{trend_detail}')
    else:
        lines.append('- **60日趋势**：数据不足')

    return lines


def generate_dingtalk_payload(date_str: str, top_picks: pd.DataFrame,
                                warnings: pd.DataFrame, all_stocks: pd.DataFrame,
                                market: Dict[str, Any] = None) -> Dict:
    """
    生成钉钉消息 payload

    钉钉支持的 markdown 子集：
    - 标题：# ## ###（最多 6 级）
    - 加粗：**text**
    - 链接：[text](url)
    - 图片：![](url)
    - 引用：> text
    - 无序列表：- text
    - 有序列表：1. text

    返回 Dict 格式：
    {
        "msgtype": "markdown",
        "markdown": {
            "title": "消息标题",
            "text": "Markdown 内容"
        }
    }
    """
    lines = []
    lines.append(f'# 🎯 主升浪精选日报 {date_str}')
    lines.append('')
    lines.append(f'> 📡 数据源：Tushare')
    lines.append(f'> 🕐 生成时间：{datetime.now().strftime("%H:%M")}')
    lines.append(screen_footnote())
    lines.append('')

    # 大盘环境（基于上证指数）
    if market is not None:
        lines.append('## 📊 大盘环境')
        lines.append('')
        lines.extend(generate_market_section(market))
        lines.append('')

    # 今日重点
    lines.append('## 📲 今日重点')
    lines.append('')
    points = generate_keystrokes(top_picks)
    for p in points:
        lines.append(f'- {p}')
    lines.append('')

    # TOP 表格（9 列宽表格：用户反馈此版更顺眼，钉钉手机端虽偶有堆叠但可读性更高）
    # ⚠️ 第 7 列口径：新口径 = 超大单占比（近5日净额/近5日成交额）；legacy = 当日主力净额
    lines.append(f'## 📋 TOP {len(top_picks)} 精选')
    lines.append('')
    if not top_picks.empty:
        lines.append(f'| # | 代码 | 名称 | 现价 | 当日 | **60日** | {ELG_HEAD} | 评分 | 关键 |')
        lines.append('|---|---|---|---|---|---|---|---|---|')
        for i, (_, row) in enumerate(top_picks.iterrows(), 1):
            medal = ['🥇', '🥈', '🥉'][i - 1] if i <= 3 else str(i)
            inflow = elg_cell(row)
            score = format_num(row.get('total_score'), '.0f')
            # 关键特征：取题材 + 当日涨幅
            catalyst = ''
            for k, v in row.get('score_breakdown', {}).items():
                if k == '题材':
                    catalyst = v.split(' ')[0]
                    break
            catalyst = catalyst if catalyst else f'{format_pct(row.get("pct_change", 0))}'
            lines.append(
                f'| {medal} | {row["code"]} | {row["name"]} | '
                f'{format_price(row["price"])} | {format_pct(row["pct_change"])} | '
                f'**{format_pct(row.get("pct_60d", 0))}** | '
                f'{inflow} | {score} | {catalyst} |'
            )
        lines.append('')

    # 操作指引（差异化：根据每只股票的特征生成针对性建议）
    if not top_picks.empty:
        lines.append('## ⚡ 操作指引')
        lines.append('')
        for i, (_, row) in enumerate(top_picks.iterrows(), 1):
            medal = ['🥇', '🥈', '🥉'][i - 1] if i <= 3 else f'{i}'
            tips = generate_action_tips(row)
            lines.append(f'**{medal} {row["name"]}** {tips}')
        lines.append('')

    # 板块热度 TOP3（识别资金聚焦方向）
    theme_lines = generate_theme_heatmap(top_picks, all_stocks)
    if theme_lines:
        lines.append('## 🔥 板块热度 TOP3')
        lines.append('')
        for t in theme_lines:
            lines.append(f'- {t}')
        lines.append('')

    # 警示名单
    if not warnings.empty:
        lines.append(f'## ⚠️ 警示名单（高位派发段，请勿追）')
        lines.append('')
        lines.append('| 代码 | 名称 | 累计涨幅 | 风险点 |')
        lines.append('|---|---|---|---|')
        for _, row in warnings.iterrows():
            pct = format_pct(row.get('pct_5d', row.get('pct_change', 0)))
            reasons = []
            if row.get('pct_5d', 0) >= 30 or row.get('pct_10d', 0) >= 50:
                reasons.append('超买')
            if row.get('turnover_rate', 0) >= 30:
                reasons.append('换手过热')
            if row.get('main_net_inflow', 0) < -1e8:
                reasons.append('主力流出')
            reason = '/'.join(reasons) if reasons else '见K线'
            lines.append(f'| {row["code"]} | {row["name"]} | {pct} | {reason} |')
        lines.append('')

    # 板块联动观察
    if not all_stocks.empty:
        lines.append('## 📊 当日观察')
        lines.append('')
        # 涨停家数
        limit_count = len(all_stocks[all_stocks['pct_change'] >= 9.5])
        if limit_count > 0:
            lines.append(f'- 涨停家数：**{limit_count}**')
        # 平均换手（可选展示项：缺列时降级跳过，不让整条推送失败）
        avg_turnover = (all_stocks['turnover_rate'].mean()
                        if 'turnover_rate' in all_stocks.columns else float('nan'))
        if not pd.isna(avg_turnover):
            lines.append(f'- 市场平均换手：{avg_turnover:.2f}%')
        lines.append('')

    # 免责声明
    lines.append('---')
    lines.append('')
    lines.append('⚠️ **免责声明**：以上内容由 AI 基于 Tushare 公开数据生成，'
                 '仅供参考，不构成任何投资建议。投资有风险，决策需谨慎。')

    text = '\n'.join(lines)

    return {
        'msgtype': 'markdown',
        'markdown': {
            'title': f'📊 主升浪精选 {date_str}',
            'text': text,
        }
    }


def _sector_leaders_outlook(leaders: Dict[str, List[Dict]]) -> List[str]:
    """三榜（涨幅 / 资金 / 涨停）→ 明日展望要点。

    【任务②】三条独立陈述，避免把「资金榜第一」说成「情绪第一」；
    若三榜头名不一致，额外点明这是轮动特征（只报一个混合名次会误导）。
    """
    lines = []
    g = leaders.get('gainers') or []
    f = leaders.get('flows') or []
    u = leaders.get('limit_ups') or []
    g0 = g[0] if g else None
    f0 = f[0] if f else None
    u0 = u[0] if u else None

    if g0 is not None:
        lines.append(
            f'- 涨幅领先：**{g0["theme"]}**（平均 {g0["avg_pct"]:+.1f}%，{g0["cnt"]} 只成分）'
            f' → 关注该板块内**强势股的次日溢价**'
        )
    if f0 is not None:
        fy = safe_float(f0.get('inflow_yi'))
        if fy is None:
            lines.append(f'- 资金榜头名：**{f0["theme"]}**（主力流入数据缺失）')
        elif fy > 0:
            lines.append(
                f'- 主力资金聚焦：**{f0["theme"]}**（{fy:+.1f}亿，平均 {f0["avg_pct"]:+.1f}%）'
                f' → 资金与情绪同向，可优先跟踪'
            )
        else:
            lines.append(
                f'- ⚠️ 资金榜头名 **{f0["theme"]}** 仍为**净流出 {abs(fy):.1f}亿** → '
                f'全市场无板块呈净流入，情绪强于资金，追高需谨慎'
            )
    if u0 is not None and u0.get('limit_up', 0) > 0:
        lines.append(
            f'- 涨停家数最多：**{u0["theme"]}**（{u0["limit_up"]} 只，平均 {u0["avg_pct"]:+.1f}%）'
            f' → 短线情绪最集中处，注意**分歧转一致**的节奏'
        )

    names = [x['theme'] for x in (g0, f0, u0) if x is not None]
    if len(set(names)) > 1:
        lines.append(
            f'- 三榜头名不一致（{" / ".join(names)}）→ 属**板块轮动**特征，宜分散跟踪、不宜单押'
        )
    return lines


def _render_sector_leaders(leaders: Dict[str, List[Dict]]) -> List[str]:
    """三榜 → markdown 行。

    推送版与落盘版**共用同一实现**（2026-09-15 曾因双份实现只改一处而漏配色）。
    """
    def _row(i, item, main_txt, sub_txt):
        medal = ['🥇', '🥈', '🥉'][i] if i < 3 else str(i + 1)
        return f'- {medal} **{item["theme"]}** - {main_txt} ｜ {sub_txt}'

    out = ['## 🔥 板块三榜', '']
    gainers = leaders.get('gainers') or []
    if gainers:
        out.append('**📈 涨幅榜**')
        for i, it in enumerate(gainers):
            out.append(_row(
                i, it,
                f'{pct_color(it.get("avg_pct"))}平均涨幅 {format_num(it.get("avg_pct"), "+.2f", "%")}',
                f'{it.get("cnt", 0)} 只成分'))
        out.append('')

    flows = leaders.get('flows') or []
    if flows:
        out.append('**💰 资金榜**')
        for i, it in enumerate(flows):
            out.append(_row(
                i, it,
                f'主力 {format_num(it.get("inflow_yi"), "+.1f", "亿")}',
                f'{pct_color(it.get("avg_pct"))}平均涨幅 {format_num(it.get("avg_pct"), "+.2f", "%")}'))
        out.append('')

    ups = leaders.get('limit_ups') or []
    if ups:
        out.append('**🚀 涨停榜**')
        for i, it in enumerate(ups):
            out.append(_row(
                i, it,
                f'涨停 **{it.get("limit_up", 0)}只**',
                f'{pct_color(it.get("avg_pct"))}平均涨幅 {format_num(it.get("avg_pct"), "+.2f", "%")}'))
        out.append('')

    cov = leaders.get('coverage') or {}
    if cov:
        note = (f'覆盖 {cov.get("tagged", 0)}/{cov.get("total", 0)} 只'
                f'（{cov.get("ratio", 0)}%）｜ {cov.get("n_sector", 0)} 个板块')
        if cov.get('industry_downgraded'):
            note += ' ｜ ⚠️ 行业兜底不可用，已回退纯题材匹配'
        out.append(f'> 口径：三榜**独立排序**（涨幅 / 主力净流入 / 涨停家数），不再混合加权。{note}')
    out.append('')
    return out


def generate_outlook(market: Dict[str, Any], sector_heat: List[Dict],
                     sector_leaders: Dict[str, List[Dict]] = None) -> List[str]:
    """根据大盘趋势与板块方向生成『明日展望』要点。

    【任务②】传了 sector_leaders 就走三榜逻辑（涨幅/资金/涨停分别陈述），
    否则回退旧的混合强度榜逻辑（向后兼容）。
    """
    lines = []
    trend = market.get('trend_60d', 'unknown')
    main_yi = market.get('main_net_inflow_yi')

    # 1) 大盘趋势判断
    if trend == 'up':
        lines.append('- 大盘处于**上升通道**，可关注强势板块的**低吸**机会')
    elif trend == 'down':
        lines.append('- 大盘**走弱**，建议**控制仓位**，谨慎追高')
    elif trend == 'sideways':
        lines.append('- 大盘**震荡整理**，以**板块轮动**思路操作，高抛低吸')
    else:
        lines.append('- 大盘趋势**待确认**，建议观望为主')

    # 2) 主力资金方向
    if main_yi is not None:
        if main_yi > 50:
            lines.append(f'- 主力资金**大幅流入**（{main_yi:+.0f}亿），做多情绪回暖')
        elif main_yi > 0:
            lines.append(f'- 主力资金**温和流入**（{main_yi:+.0f}亿），题材有承接')
        elif main_yi > -50:
            lines.append(f'- 主力资金**小幅流出**（{main_yi:+.0f}亿），注意规避高位板块')
        else:
            lines.append(f'- 主力资金**大幅流出**（{main_yi:+.0f}亿），谨防踩踏')

    # 3) 板块方向
    # 【任务②】优先用三榜（涨幅/资金/涨停 独立排序，口径透明、无参数争议）；
    #          未传三榜时回退旧的混合强度榜（向后兼容）。
    if sector_leaders:
        lines.extend(_sector_leaders_outlook(sector_leaders))
    # ⚠️ 旧路径：sector_heat 按「强度分」排序（= 平均涨幅*0.4 + 主力流入*0.3 + 涨停数*0.3），
    #    并不等价于「资金净流入方向」。若无条件写"资金聚焦 X"，会出现
    #    X 实际主力净流出却被描述成资金流入的误导（2026-09-15 用户截图暴露：
    #    半导体 温度第 1，但主力 -18.3 亿）。故按 top 的资金方向分三种措辞。
    elif sector_heat:
        top = sector_heat[0]
        top_theme = top.get('theme', '未知')
        top_yi = safe_float(top.get('inflow_yi'))
        top_pct = safe_float(top.get('avg_pct'))
        pct_txt = f'{top_pct:+.1f}%' if top_pct is not None else '-'
        if top_yi is None:
            lines.append(
                f'- **{top_theme}** 板块温度居首（平均涨幅 {pct_txt}），'
                f'明日可优先跟踪该板块的**龙头分歧转一致**机会'
            )
        elif top_yi > 0:
            lines.append(
                f'- 资金聚焦 **{top_theme}**（平均涨幅 {pct_txt}，主力 {top_yi:+.1f}亿），'
                f'明日可优先跟踪该板块的**龙头分歧转一致**机会'
            )
        else:
            lines.append(
                f'- **{top_theme}** 板块温度居首（平均涨幅 {pct_txt}），'
                f'但主力**净流出 {abs(top_yi):.1f}亿** → 情绪强于资金，追高需谨慎'
            )
            # 温度第一 ≠ 资金第一：若另有板块资金真在流入，点明真正的资金去向
            inflow_best = max(
                (s for s in sector_heat
                 if (safe_float(s.get('inflow_yi')) or 0) > 0),
                key=lambda s: safe_float(s.get('inflow_yi')) or 0,
                default=None,
            )
            if inflow_best is not None and inflow_best.get('theme') != top_theme:
                by = safe_float(inflow_best.get('inflow_yi')) or 0
                lines.append(
                    f'- 资金实际流入方向是 **{inflow_best.get("theme", "未知")}**'
                    f'（主力 {by:+.1f}亿），可对比跟踪'
                )

    # 4) 风险提示
    if main_yi is not None and main_yi < -50:
        lines.append('')
        lines.append('> ⚠️ 大盘资金面偏弱，建议压缩仓位、精选确定性标的')

    return lines


def format_date_cn(date_str: str) -> str:
    """YYYYMMDD → YYYY-MM-DD；空值返回 '-'"""
    if not date_str:
        return '-'
    s = str(date_str)
    if len(s) == 8 and s.isdigit():
        return f'{s[:4]}-{s[4:6]}-{s[6:]}'
    return s


def format_md_date(date_str: str) -> str:
    """YYYY-MM-DD / YYYYMMDD → MM-DD（用于表格窄列）"""
    s = format_date_cn(date_str)
    return s[5:] if len(s) >= 10 else s


def generate_holdings_section(holdings_status: List[Dict] = None,
                              closed_today: List[Dict] = None,
                              new_positions: List[Dict] = None,
                              trail_started: List[Dict] = None,
                              stats: Dict = None,
                              max_holdings: int = _MAX_HOLDINGS,
                              stop_loss_pct: float = _STOP_LOSS_PCT,
                              exit_use_stop: bool = _EXIT_USE_STOP,
                              exit_use_ma: bool = _EXIT_USE_MA,
                              exit_ma_half: int = _EXIT_MA_HALF,
                              exit_ma_clear: int = _EXIT_MA_CLEAR,
                              trail_enabled: bool = _TRAIL_ENABLED,
                              trail_activate_pct: float = _TRAIL_ACTIVATE_PCT,
                              trail_drawdown_pct: float = _TRAIL_DRAWDOWN_PCT,
                              time_stop_enabled: bool = _TIME_STOP_ENABLED,
                              zombie_days: int = _ZOMBIE_DAYS,
                              zombie_peak_pct: float = _ZOMBIE_PEAK_PCT,
                              inefficient_days: int = _INEFFICIENT_DAYS,
                              inefficient_peak_pct: float = _INEFFICIENT_PEAK_PCT,
                              market_fuse_enabled: bool = _MARKET_FUSE_ENABLED,
                              market_fuse_index: str = _MARKET_FUSE_INDEX,
                              market_fuse_drop_pct: float = _MARKET_FUSE_DROP_PCT,
                              market_fuse_block_refill: bool = _MARKET_FUSE_BLOCK_REFILL) -> List[str]:
    """
    生成「模拟盘持仓」markdown 段（离场结算 / 减半仓 / 自动补仓结果）

    - holdings_status：当前活跃持仓的评估结果列表
    - closed_today：当日触发清仓（止损 / 破MA20 / 移动止盈 / 时间止损）的持仓
    - new_positions：当日自动补仓的新增持仓
    - trail_started：当日新启动移动止盈的持仓（仅 TRAIL_ENABLED 时可能有值）

    ★ 离场口径（2026-09-22 起，大波段）：固定止损 −7% ＋ 破 MA10 减半 ＋ 破 MA20 清仓，
      熔断（上证 ≤ −2%）当天只暂缓「破 MA10 减半」并暂停补仓。
    """
    rows = holdings_status or []
    closed_today = closed_today or []
    new_positions = new_positions or []
    trail_started = trail_started or []

    lines = [f'## 💼 模拟盘持仓（{len(rows)}/{max_holdings}）', '']

    if rows:
        dist_col = (f'距MA{exit_ma_half}/MA{exit_ma_clear}' if exit_use_ma else '距止盈')
        lines.append('| # | 代码 | 名称 | 持有 | 买入价 | 现价 | 盈亏 | 峰涨 | 距止损 | '
                     + dist_col + ' | 状态 |')
        lines.append('|---|---|---|---|---|---|---|---|---|---|---|')
        for i, r in enumerate(rows, 1):
            trail_active = bool(r.get('trail_active'))
            if r.get('entry_pending'):
                status = '⏳ 待开盘价'
            elif r.get('fused'):
                status = r.get('status_text') or '⚡ 熔断暂缓'
            elif r.get('trigger') == 'ma10_half':
                status = r.get('status_text') or f'✂️ 破MA{exit_ma_half} 减半仓'
            elif r.get('half_sold'):
                status = f'◐ 半仓持有（已破MA{exit_ma_half}）'
            elif trail_active and r.get('trail_high'):
                # 已启动移动止盈 → 状态里带上峰值，一眼看出锁利位置
                status = f"🔒 移动止盈(峰{r['trail_peak_pnl']:+.0f}%)"
            elif r.get('trigger') == 'warning':
                status = r.get('status_text') or '⚡ 接近启动线'
            elif r.get('time_stop_hint'):
                # 未启动且「没表现」→ 时间止损观察倒计时
                status = r['time_stop_hint']
            else:
                status = r.get('status_text') or '🟢 正常持有'
            # 持有交易日数
            days = r.get('days_held')
            days_txt = f'{int(days)}日' if days is not None else '-'
            # 期间最高涨幅
            peak_pnl = r.get('peak_high_pnl')
            peak_txt = f'{peak_pnl:+.1f}%' if peak_pnl is not None else '-'
            # 距离场闸门：MA 模式＝距 MA10 / MA20；旧模式＝距止盈线
            if exit_use_ma:
                g10, g20 = r.get('ma10_gap_pct'), r.get('ma20_gap_pct')
                d10 = f'{g10:+.1f}%' if g10 is not None else '-'
                d20 = f'{g20:+.1f}%' if g20 is not None else '-'
                dist_txt = f'{d10} / {d20}'
            else:
                dist_tp = r.get('distance_to_take_profit')
                dist_txt = f'{dist_tp:+.1f}%' if dist_tp is not None else '-'
            lines.append(
                f"| {i} | {r['code']} | {r['name']} | {days_txt} | "
                f"{r['buy_price']:.2f} | {r['current_price']:.2f} | "
                f"**{r['pnl_pct']:+.2f}%** | {peak_txt} | "
                f"{r['distance_to_stop_loss']:+.1f}% | {dist_txt} | {status} |"
            )
        lines.append('')
        lines.append(f'> 💡 买入价＝信号次日开盘价（模拟盘口径）')
        rules = []
        if exit_use_stop:
            rules.append(f'收盘 ≤{stop_loss_pct}% **固定止损**')
        if exit_use_ma:
            rules.append(f'跌破 MA{exit_ma_half} **卖出 50%**（减半后不补仓）')
            rules.append(f'跌破 MA{exit_ma_clear} **全部清仓**')
        if trail_enabled:
            rules.append(f'盈利 +{trail_activate_pct}% 启动移动止盈（回撤 {trail_drawdown_pct}% 卖出）')
        if rules:
            lines.append('> 📐 离场规则：' + ' ｜ '.join(rules))
        if time_stop_enabled:
            lines.append(f'> 🧹 时间止损：持有 ≥{zombie_days}日且期间最高涨幅 <{zombie_peak_pct}% → 🧟 僵尸股清理；'
                         f'≥{inefficient_days}日且期间最高 <{inefficient_peak_pct}% → 🐌 低效股清理')
        if market_fuse_enabled:
            lines.append(f'> ⚡ 大盘熔断：{market_fuse_index} 单日跌幅 ≤{market_fuse_drop_pct}% → '
                         f'当天**只暂缓「破MA{exit_ma_half}减半」**'
                         + ('＋暂停补仓' if market_fuse_block_refill else '')
                         + f'；固定止损与破MA{exit_ma_clear}清仓**照常执行**')
        lines.append('')

    # 今日减半仓（破 MA10 → 卖出 50%，仍持有剩余仓位）
    half_today = [r for r in rows if r.get('trigger') == 'ma10_half' and not r.get('fused')]
    if half_today:
        lines.append(f'### ✂️ 今日破 MA{exit_ma_half} 减半仓')
        lines.append('')
        for r in half_today:
            pnl = r.get('half_pnl_pct')
            pnl_txt = f'{pnl:+.2f}%' if pnl is not None else '-'
            lines.append(
                f"- ✂️ **{r.get('name')}({r.get('code')})** 卖出 50% **{pnl_txt}**"
                f"（现价 {r.get('half_price')} < MA{exit_ma_half} {r.get('ma10')}）"
                f"→ 剩余 {int((r.get('remaining_ratio') or 0.5) * 100)}% 继续持有，**不补仓**"
            )
        lines.append('')

    # 熔断暂缓（破MA10减半 / 时间止损）
    fused_rows = [r for r in rows if r.get('fused')]
    if fused_rows:
        lines.append('### ⚡ 大盘熔断·今日暂缓')
        lines.append('')
        for r in fused_rows:
            lines.append(f"- ⚡ **{r.get('name')}({r.get('code')})** {r.get('status_text')}"
                         f"（现价 {r.get('current_price')} ｜ MA{exit_ma_half} {r.get('ma10')}）"
                         f"→ 今日不动作，大盘企稳后自动补执行")
        lines.append('> ⚠️ 熔断**不影响**固定止损与破 MA 清仓 —— 那是保护性纪律。')
        lines.append('')

    # 今日新启动移动止盈
    if trail_started:
        lines.append('### 🔒 今日启动移动止盈')
        lines.append('')
        for r in trail_started:
            lines.append(
                f"- 🔒 **{r.get('name')}({r.get('code')})** 峰值 {r.get('trail_high'):.2f}"
                f"（{r.get('trail_peak_pnl'):+.1f}%）→ 回撤线 **{r.get('trail_trigger_price'):.2f}**"
            )
        lines.append('')
        lines.append(f'> 说明：触发线随峰值上移（只上不下），跌破回撤线即卖出。')
        lines.append('')

    # 按清仓原因拆分：普通出场（止损/破MA20/移动止盈） vs 时间止损（清理让位）
    exits = [h for h in closed_today
             if h.get('close_reason') in ('take_profit', 'stop_loss', 'ma20_exit')]
    time_stops = [h for h in closed_today
                  if str(h.get('close_reason') or '').startswith('time_stop')]

    # 今日触发（固定止损 / 破 MA20 清仓 / 移动止盈）
    if exits:
        lines.append('### 🚨 今日清仓')
        lines.append('')
        for h in exits:
            reason = h.get('close_reason')
            peak = h.get('closed_peak_pnl')
            if reason == 'take_profit':
                icon, label = '🔒', '移动止盈卖出'
                if peak is not None:
                    label += f'（峰值 {peak:+.1f}%）'
            elif reason == 'ma20_exit':
                icon, label = '🔻', f'跌破 MA{exit_ma_clear} 清仓'
            else:
                icon, label = '🚨', '触发固定止损'
            pnl = h.get('closed_pnl')
            pnl_txt = f'{pnl:+.2f}%' if pnl is not None else '-'
            price_txt = ''
            if h.get('closed_price'):
                price_txt = f'（{h.get("buy_price"):.2f} → {h["closed_price"]:.2f}）'
            half_txt = ''
            if h.get('half_sold'):
                half_txt = (f' ◐加权口径：半仓段 **{h.get("half_sold_pnl"):+.2f}%**'
                            f'×{h.get("half_ratio")} + 尾段 **{h.get("closed_pnl_leg2"):+.2f}%**')
            lines.append(f'- {icon} **{h.get("name")}({h.get("code")})** {label} '
                         f'**{pnl_txt}**{price_txt} → 已清仓{half_txt}')
        lines.append('')

    # 今日时间止损（僵尸股 / 低效股清理 → 腾出仓位给新机会）
    if time_stops:
        lines.append('### 🧹 今日时间止损（清理让位）')
        lines.append('')
        for h in time_stops:
            reason = h.get('close_reason')
            icon = '🧟' if reason == 'time_stop_zombie' else '🐌'
            label = '僵尸股' if reason == 'time_stop_zombie' else '低效股'
            peak = h.get('closed_peak_pnl')
            days = h.get('closed_days_held')
            detail = []
            if days is not None:
                detail.append(f'持有 {days} 交易日')
            if peak is not None:
                detail.append(f'期间最高 {peak:+.1f}%')
            detail_txt = f"（{' / '.join(detail)}）" if detail else ''
            pnl = h.get('closed_pnl')
            pnl_txt = f'{pnl:+.2f}%' if pnl is not None else '-'
            price_txt = ''
            if h.get('closed_price'):
                price_txt = f'（{h.get("buy_price"):.2f} → {h["closed_price"]:.2f}）'
            lines.append(f'- {icon} **{h.get("name")}({h.get("code")})** {label}清理{detail_txt} '
                         f'**{pnl_txt}**{price_txt} → 已清仓')
        lines.append('')
        lines.append('> 🧹 清理腾出的仓位，已由下方「今日补仓」用当日强势股补齐。')
        lines.append('')

    # 今日补仓
    if new_positions:
        lines.append('### 🛒 今日补仓（自动补位）')
        lines.append('')
        for h in new_positions:
            lines.append(
                f'- ➕ **{h.get("name")}({h.get("code")})** 参考价 {h.get("buy_price"):.2f} 元'
                f'（次日开盘价成交）'
            )
        lines.append('')

    # 累计战绩
    if stats and stats.get('total'):
        lines.append('### 📈 累计战绩')
        lines.append('')
        lines.append(
            f"- 已平仓 **{stats['total']}** 笔 ｜ 胜率 **{stats['win_rate']}%** "
            f"｜ 平均收益 **{stats['avg_pnl']:+.2f}%**"
        )
        parts = []
        if stats.get('stop_losses'):
            parts.append(f"🚨 固定止损 {stats['stop_losses']}")
        if stats.get('ma20_exits'):
            parts.append(f"🔻 破MA{exit_ma_clear}清仓 {stats['ma20_exits']}")
        if stats.get('half_exits'):
            parts.append(f"✂️ 含减半 {stats['half_exits']}")
        if stats.get('take_profits'):
            parts.append(f"🔒 移动止盈 {stats['take_profits']}")
        if stats.get('time_stops'):
            parts.append(f"🧹 时间止损 {stats['time_stops']}")
        if stats.get('avg_days_held') is not None:
            parts.append(f"平均持有 {stats['avg_days_held']} 交易日")
        if parts:
            lines.append(f"- {' ｜ '.join(parts)}")
        lines.append('> 📐 统计口径：一笔「先减半、后清仓」的交易**合并为 1 笔**，'
                     '收益按 50%×半仓段 + 50%×尾段加权。')
        lines.append('')

    if not rows and not closed_today and not new_positions:
        lines.append('> 📭 当前空仓，等待下一次选股/补仓。')
        lines.append('')

    return lines


def generate_review_payload(date_str: str, top_picks: pd.DataFrame,
                            warnings: pd.DataFrame, all_stocks: pd.DataFrame,
                            market: Dict[str, Any] = None,
                            sector_heat: List[Dict] = None,
                            sector_leaders: Dict[str, List[Dict]] = None,
                            holdings_status: List[Dict] = None,
                            closed_today: List[Dict] = None,
                            new_positions: List[Dict] = None,
                            trail_started: List[Dict] = None,
                            stats: Dict = None,
                            data_date: str = None) -> Dict:
    """
    生成「主升浪盘后复盘」钉钉消息 payload（18:30 推送）
    核心模块：
    - 大盘复盘（指数/主力/趋势/成交额/市场情绪）
    - 💼 模拟盘持仓（移动止盈结算 + 自动补仓结果）—— 已合并原「持仓监控」
    - 今日强势股 TOP5（收盘后五维评分重筛）
    - 板块三榜（【任务②】涨幅 / 主力资金 / 涨停家数，独立排序）
    - 明日展望
    """
    lines = []
    lines.append(f'# 📊 主升浪盘后复盘 {date_str}')
    lines.append('')
    lines.append(f'> 📡 数据源：Tushare')
    lines.append(f'> 📅 数据日期：{format_date_cn(data_date) if data_date else date_str}')
    lines.append(f'> 🕐 生成时间：{datetime.now().strftime("%H:%M")} · 视角：当日复盘 + 持仓结算')
    lines.append('')

    # ---- 大盘复盘 ----
    lines.append('## 📊 大盘复盘')
    lines.append('')
    if market is not None:
        lines.extend(generate_market_section(market))

        amount_yi = market.get('amount_yi')
        if amount_yi is not None:
            if amount_yi >= 12000:
                vol_label = f'量能充沛' + ('' if amount_yi >= 15000 else '（偏高）')
            elif amount_yi >= 8000:
                vol_label = '量能温和'
            else:
                vol_label = '量能偏弱'
            lines.append(f'- **两市成交额**：{amount_yi:.0f} 亿（{vol_label}）')
        else:
            lines.append('- **两市成交额**：数据缺失')

        # 市场情绪：涨跌停家数 + 涨跌家数
        lu = market.get('limit_up_count')
        ld = market.get('limit_down_count')
        up = market.get('up_count')
        down = market.get('down_count')
        if lu is not None and up is not None:
            lines.append(f'- **市场情绪**：涨停 {lu} ｜ 跌停 {ld or 0} ｜ 涨跌比 {up}:{down}')
        lines.append('')

    # ---- 💼 模拟盘持仓（移动止盈结算 + 自动补仓）----
    lines.extend(generate_holdings_section(
        holdings_status=holdings_status,
        closed_today=closed_today,
        new_positions=new_positions,
        trail_started=trail_started,
        stats=stats,
    ))

    # ---- 今日强势股 TOP5（收盘后）----
    lines.append(f'## 📋 今日强势股 TOP {len(top_picks)}（收盘后）')
    lines.append('')
    if not top_picks.empty:
        lines.append(f'| # | 代码 | 名称 | 现价 | 当日 | {ELG_HEAD} | 评分 | 关键 |')
        lines.append('|---|---|---|---|---|---|---|---|')
        for i, (_, row) in enumerate(top_picks.iterrows(), 1):
            medal = ['🥇', '🥈', '🥉'][i - 1] if i <= 3 else str(i)
            inflow = elg_cell(row)
            score = format_num(row.get('total_score'), '.0f')
            catalyst = ''
            for k, v in row.get('score_breakdown', {}).items():
                if k == '题材':
                    catalyst = v.split(' ')[0]
                    break
            catalyst = catalyst if catalyst else f'{format_pct(row.get("pct_change", 0))}'
            lines.append(
                f'| {medal} | {row["code"]} | {row["name"]} | '
                f'{format_price(row["price"])} | {format_pct(row["pct_change"])} | '
                f'{inflow} | {score} | {catalyst} |'
            )
        lines.append('')

    # ---- 板块方向（【任务②】三榜优先；未传三榜则回退旧混合榜）----
    if sector_leaders:
        lines.extend(_render_sector_leaders(sector_leaders))
    elif sector_heat:
        lines.append('## 🔥 板块温度 TOP3')
        lines.append('')
        for i, sector in enumerate(sector_heat, 1):
            medal = ['🥇', '🥈', '🥉'][i - 1] if i <= 3 else str(i)
            # A 股惯例：涨＝红、跌＝绿（与国际市场相反，勿反）
            emoji_up = pct_color(sector['avg_pct'])
            lines.append(
                f'- {medal} **{sector["theme"]}** - {emoji_up}平均涨幅 {sector["avg_pct"]:+.1f}% ｜ '
                f'主力 {sector["inflow_yi"]:+.1f}亿 ｜ 涨停 {sector["limit_up"]}只'
            )
        # 排序口径透明化：否则用户会疑惑"为什么下跌的板块排在上涨的前面"
        lines.append('')
        lines.append('> 排序口径：板块强度 = 平均涨幅×0.4 + 主力净流入×0.3 + 涨停数×0.3')
        lines.append('')

    # ---- 明日展望 ----
    lines.append('## 💡 明日展望')
    lines.append('')
    outlook = generate_outlook(market or {}, sector_heat or [], sector_leaders)
    for o in outlook:
        lines.append(o)
    lines.append('')

    # ---- 免责声明 ----
    lines.append('---')
    lines.append('')
    lines.append('⚠️ **免责声明**：以上内容由 AI 基于 Tushare 公开数据自动生成，'
                 '仅供参考，不构成任何投资建议。投资有风险，决策需谨慎。')

    text = '\n'.join(lines)
    return {
        'msgtype': 'markdown',
        'markdown': {
            'title': f'📊 主升浪复盘 {date_str}',
            'text': text,
        }
    }


def save_full_report(date_str: str, top_picks: pd.DataFrame,
                    warnings: pd.DataFrame, all_stocks: pd.DataFrame,
                    reports_dir: Path,
                    market: Dict[str, Any] = None) -> str:
    """
    生成完整版 Markdown 报告（保存到 reports/ 目录做历史记录）
    """
    lines = []
    lines.append(f'# 🎯 主升浪精选日报 {date_str}')
    lines.append('')
    lines.append(f'> 数据时间：{datetime.now().strftime("%Y-%m-%d %H:%M:%S")} · 数据源：Tushare')
    lines.append(screen_footnote().replace('> 🎯 ', '> ').replace('主板非ST', '沪深主板 / 非ST'))
    lines.append('')

    # 大盘环境
    if market is not None:
        lines.append('## 📊 大盘环境（上证指数）')
        lines.append('')
        lines.extend(generate_market_section(market))
        lines.append('')

    # 评分维度说明
    lines.append('## 🎯 评分维度（满分 100）')
    lines.append('')
    lines.append('| 维度 | 权重 | 评分要点 |')
    lines.append('|---|---|---|')
    lines.append('| 技术面 | 25% | 当日涨幅、5日涨幅、60日趋势、量比、**趋势线(MA120)** |')
    lines.append('| 资金面 | 20% | ' + (
        ('**超大单5日/60日趋势**、超大单占成交额、换手率'
         + (f'（单日占比≤{CAPITAL_DAY_RATIO_CAP:g}%）'
            if CAPITAL_DAY_RATIO_CAP and CAPITAL_DAY_RATIO_CAP > 0 else ''))
        if CAPITAL_MODE == 'elg' else '主力净流入、换手率') + ' |')
    lines.append('| 估值 | 15% | PE-TTM、PB |')
    lines.append('| 题材催化 | 15% | 热门主题、当日关注度 |')
    lines.append('| 基本面 | 25% | 业绩（PE 间接）、市值、稳定性 |')
    lines.append('')

    # TOP 详细
    lines.append(f'## 📋 TOP {len(top_picks)} 精选')
    lines.append('')
    if not top_picks.empty:
        for i, (_, row) in enumerate(top_picks.iterrows(), 1):
            medal = ['🥇', '🥈', '🥉'][i - 1] if i <= 3 else f'**{i}**'
            lines.append(f'### {medal} {row["name"]} ({row["code"]})')
            lines.append('')
            lines.append(f'- 现价：**{format_price(row["price"])}** 元')
            lines.append(f'- 当日涨幅：{format_pct(row["pct_change"])}')
            lines.append(f'- 60日涨跌幅：{format_pct(row.get("pct_60d", 0))}')
            lines.append(f'- 换手率：{format_num(row.get("turnover_rate"), ".2f", "%")}')
            lines.append(f'- 量比：{format_num(row.get("volume_ratio"), ".2f")}')
            lines.append(f'- PE-TTM：{format_num(row.get("pe_ttm"), ".1f")}')
            lines.append(f'- 流通市值：{format_mcap_yi(row.get("circ_mcap"))}')
            lines.extend(capital_detail_lines(row))
            lines.append('')
            lines.append(f'**综合评分：{format_num(row.get("total_score"), ".0f")}/100**')
            lines.append('')
            breakdown = row.get('score_breakdown', {})
            lines.append('<details>')
            lines.append('<summary>📊 评分明细</summary>')
            lines.append('')
            for k, v in breakdown.items():
                lines.append(f'- {k}：{v}')
            lines.append('')
            lines.append('</details>')
            lines.append('')
            lines.append('> ⚠️ **风险提示**：本数据基于公开行情生成，请结合大盘环境、行业政策、个股公告综合判断。')
            lines.append('> 💡 **建议**：关注开盘后强弱，回调时分批小仓试探；严格执行止损纪律。')
            lines.append('')

    # 警示名单
    if not warnings.empty:
        lines.append(f'## ⚠️ 警示名单')
        lines.append('')
        lines.append('以下标的虽触发初筛条件，但因累计涨幅过大、换手过热或主力流出，存在追高风险。')
        lines.append('')
        lines.append('| # | 代码 | 名称 | 现价 | 累计涨幅 | 风险点 |')
        lines.append('|---|---|---|---|---|---|')
        for i, (_, row) in enumerate(warnings.iterrows(), 1):
            pct = format_pct(row.get('pct_5d', row.get('pct_change', 0)))
            reasons = []
            if row.get('pct_5d', 0) >= 30 or row.get('pct_10d', 0) >= 50:
                reasons.append('超买')
            if row.get('turnover_rate', 0) >= 30:
                reasons.append('换手过热')
            if row.get('main_net_inflow', 0) < -1e8:
                reasons.append('主力流出')
            reason = '/'.join(reasons) if reasons else '见K线'
            lines.append(
                f'| {i} | {row["code"]} | {row["name"]} | '
                f'{format_price(row["price"])} | {pct} | {reason} |'
            )
        lines.append('')

    # 当日市场观察
    if not all_stocks.empty:
        lines.append('## 📊 当日市场观察')
        lines.append('')
        limit_count = len(all_stocks[all_stocks['pct_change'] >= 9.5])
        if limit_count > 0:
            lines.append(f'- **涨停家数**：{limit_count}')
        avg_turnover = all_stocks['turnover_rate'].mean()
        if not pd.isna(avg_turnover):
            lines.append(f'- **市场平均换手**：{avg_turnover:.2f}%')
        # 资金面合计：按口径分派；**列缺失时不打印该行**（不抛异常，兼容裁剪后的候选池）
        if CAPITAL_MODE == 'elg':
            if 'elg_5d_sum' in all_stocks.columns:
                _elg_total = pd.to_numeric(all_stocks['elg_5d_sum'],
                                           errors='coerce').sum() / 1e4   # 万元 → 亿元
                if not pd.isna(_elg_total):
                    lines.append(f'- **候选池超大单 5 日净额合计**：{_elg_total:+.1f}亿')
        elif 'main_net_inflow' in all_stocks.columns:
            _mnf_total = pd.to_numeric(all_stocks['main_net_inflow'],
                                       errors='coerce').sum() / 1e8
            if not pd.isna(_mnf_total):
                lines.append(f'- **主力净流入合计**：{_mnf_total:+.1f}亿')
        lines.append('')

    # 免责声明
    lines.append('---')
    lines.append('')
    lines.append('⚠️ **免责声明**：以上内容由 AI 基于 Tushare 公开数据自动生成，仅供参考，'
                 '不构成任何投资建议。投资有风险，决策需谨慎。')

    content = '\n'.join(lines)

    # 保存
    report_path = reports_dir / f'主升浪精选_{date_str}.md'
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(content)

    return str(report_path)


def save_review_report(date_str: str, top_picks: pd.DataFrame,
                       warnings: pd.DataFrame, all_stocks: pd.DataFrame,
                       market: Dict[str, Any] = None,
                       sector_heat: List[Dict] = None,
                       sector_leaders: Dict[str, List[Dict]] = None,
                       reports_dir: Path = None,
                       holdings_status: List[Dict] = None,
                       closed_today: List[Dict] = None,
                       new_positions: List[Dict] = None,
                       trail_started: List[Dict] = None,
                       stats: Dict = None,
                       data_date: str = None) -> str:
    """
    生成完整版盘后复盘 Markdown 报告（保存到 reports/ 目录做历史记录）
    """
    if reports_dir is None:
        reports_dir = Path('reports')
    lines = []
    lines.append(f'# 📊 主升浪盘后复盘 {date_str}')
    lines.append('')
    lines.append(f'> 生成时间：{datetime.now().strftime("%Y-%m-%d %H:%M:%S")} · 数据源：Tushare')
    lines.append(f'> 数据日期：{format_date_cn(data_date) if data_date else date_str}')
    lines.append('')

    # 大盘复盘
    if market is not None:
        lines.append('## 📊 大盘复盘')
        lines.append('')
        lines.extend(generate_market_section(market))
        amount_yi = market.get('amount_yi')
        lu = market.get('limit_up_count')
        ld = market.get('limit_down_count')
        up = market.get('up_count')
        down = market.get('down_count')
        if amount_yi is not None:
            lines.append(f'- **两市成交额**：{amount_yi:.0f} 亿')
        if lu is not None and up is not None:
            lines.append(f'- **市场情绪**：涨停 {lu} ｜ 跌停 {ld or 0} ｜ 涨跌比 {up}:{down}')
        lines.append('')

    # 💼 模拟盘持仓（移动止盈结算 + 自动补仓）
    lines.extend(generate_holdings_section(
        holdings_status=holdings_status,
        closed_today=closed_today,
        new_positions=new_positions,
        trail_started=trail_started,
        stats=stats,
    ))

    # 今日强势股 TOP5
    lines.append(f'## 📋 今日强势股 TOP {len(top_picks)}（收盘后）')
    lines.append('')
    if not top_picks.empty:
        for i, (_, row) in enumerate(top_picks.iterrows(), 1):
            medal = ['🥇', '🥈', '🥉'][i - 1] if i <= 3 else f'**{i}**'
            lines.append(f'### {medal} {row["name"]} ({row["code"]})')
            lines.append('')
            lines.append(f'- 现价：**{format_price(row["price"])}** 元')
            lines.append(f'- 当日涨幅：{format_pct(row["pct_change"])}')
            lines.append(f'- 60日涨跌幅：{format_pct(row.get("pct_60d", 0))}')
            lines.append(f'- 换手率：{format_num(row.get("turnover_rate"), ".2f", "%")}')
            lines.append(f'- 量比：{format_num(row.get("volume_ratio"), ".2f")}')
            lines.append(f'- PE-TTM：{format_num(row.get("pe_ttm"), ".1f")}')
            lines.append(f'- 流通市值：{format_mcap_yi(row.get("circ_mcap"))}')
            lines.extend(capital_detail_lines(row))
            lines.append('')
            lines.append(f'**综合评分：{format_num(row.get("total_score"), ".0f")}/100**')
            lines.append('')
            breakdown = row.get('score_breakdown', {})
            lines.append('<details>')
            lines.append('<summary>📊 评分明细</summary>')
            lines.append('')
            for k, v in breakdown.items():
                lines.append(f'- {k}：{v}')
            lines.append('')
            lines.append('</details>')
            lines.append('')

    # 板块方向（【任务②】三榜优先；未传三榜则回退旧混合榜）
    if sector_leaders:
        lines.extend(_render_sector_leaders(sector_leaders))
    elif sector_heat:
        lines.append('## 🔥 板块温度 TOP3')
        lines.append('')
        for i, sector in enumerate(sector_heat, 1):
            medal = ['🥇', '🥈', '🥉'][i - 1] if i <= 3 else str(i)
            # A 股惯例配色，与推送版保持一致
            lines.append(
                f'- {medal} **{sector["theme"]}** - {pct_color(sector.get("avg_pct"))}'
                f'平均涨幅 {format_num(sector.get("avg_pct"), "+.1f", "%")} ｜ '
                f'主力 {format_num(sector.get("inflow_yi"), "+.1f", "亿")} ｜ '
                f'涨停 {sector.get("limit_up", 0)}只'
            )
        lines.append('')
        lines.append('> 排序口径：板块强度 = 平均涨幅×0.4 + 主力净流入×0.3 + 涨停数×0.3')
        lines.append('')

    # 明日展望
    lines.append('## 💡 明日展望')
    lines.append('')
    outlook = generate_outlook(market or {}, sector_heat or [], sector_leaders)
    for o in outlook:
        lines.append(o)
    lines.append('')

    # 免责声明
    lines.append('---')
    lines.append('')
    lines.append('⚠️ **免责声明**：以上内容由 AI 基于 Tushare 公开数据自动生成，仅供参考，'
                 '不构成任何投资建议。投资有风险，决策需谨慎。')

    content = '\n'.join(lines)
    report_path = reports_dir / f'主升浪复盘_{date_str}.md'
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(content)
    return str(report_path)


if __name__ == '__main__':
    print('报告生成模块独立测试需要 main.py 配合')