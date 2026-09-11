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
        TRAIL_ACTIVATE_PCT as _TRAIL_ACTIVATE_PCT,
        TRAIL_DRAWDOWN_PCT as _TRAIL_DRAWDOWN_PCT,
    )
except ImportError:  # 兼容以顶层模块方式导入
    from portfolio import (
        MAX_HOLDINGS as _MAX_HOLDINGS,
        DEFAULT_STOP_LOSS_PCT as _STOP_LOSS_PCT,
        TRAIL_ACTIVATE_PCT as _TRAIL_ACTIVATE_PCT,
        TRAIL_DRAWDOWN_PCT as _TRAIL_DRAWDOWN_PCT,
    )


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


def generate_keystrokes(df: pd.DataFrame) -> List[str]:
    """生成「今日重点」3-5 条极简要点"""
    if df.empty:
        return ['今日未筛到符合条件的标的，建议观望']

    points = []
    # 取 TOP 1
    top1 = df.iloc[0]
    inflow1 = format_yi(top1.get('main_net_inflow', 0))
    points.append(
        f'🥇 龙头 {top1["name"]}({top1["code"]}) '
        f'主力 {inflow1}流入，评分 {top1["total_score"]:.0f}'
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
    1) 主力净流入强度
    2) 当日涨幅（避免追高）
    3) PE 估值水平
    4) 换手率（活跃度/风险）
    5) 60 日趋势稳定性
    """
    tips = []

    # 1) 主力净流入信号
    inflow = float(row.get('main_net_inflow', 0) or 0)
    inflow_yi = inflow / 1e8
    if inflow_yi >= 3:
        tips.append(f'主力强势介入(+{inflow_yi:.1f}亿)')
    elif inflow_yi >= 1:
        tips.append(f'主力净流入(+{inflow_yi:.1f}亿)')
    elif inflow_yi > 0:
        tips.append(f'主力温和流入(+{inflow_yi:.1f}亿)')
    elif inflow_yi > -0.5:
        tips.append(f'主力微流出({inflow_yi:.1f}亿)')
    else:
        tips.append(f'⚠️主力撤离({inflow_yi:.1f}亿)')

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

    # 5) 总体策略建议
    if pct >= 9.5:
        conclusion = '不建议追高'
    elif inflow_yi >= 1 and 0 < pct < 6 and pe < 50:
        conclusion = '可小仓试探'
    elif inflow_yi < -0.5 or pct >= 6:
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

    # 主力净额一行
    if main_yi is not None:
        emoji = '🟢' if main_yi >= 0 else '🔴'
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
    lines.append(f'> 🎯 筛选：主板非ST / 趋势向上 / 主力流入')
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
    lines.append(f'## 📋 TOP {len(top_picks)} 精选')
    lines.append('')
    if not top_picks.empty:
        lines.append('| # | 代码 | 名称 | 现价 | 当日 | **60日** | 主力净额 | 评分 | 关键 |')
        lines.append('|---|---|---|---|---|---|---|---|---|')
        for i, (_, row) in enumerate(top_picks.iterrows(), 1):
            medal = ['🥇', '🥈', '🥉'][i - 1] if i <= 3 else str(i)
            inflow = format_yi(row.get('main_net_inflow', 0))
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
        # 平均换手
        avg_turnover = all_stocks['turnover_rate'].mean()
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


def generate_outlook(market: Dict[str, Any], sector_heat: List[Dict]) -> List[str]:
    """根据大盘趋势与板块温度生成『明日展望』要点"""
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

    # 3) 板块温度头部
    if sector_heat:
        top = sector_heat[0]
        lines.append(
            f'- 资金聚焦 **{top["theme"]}**（平均涨幅 {top["avg_pct"]:+.1f}%），'
            f'明日可优先跟踪该板块的**龙头分歧转一致**机会'
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
                              trail_activate_pct: float = _TRAIL_ACTIVATE_PCT,
                              trail_drawdown_pct: float = _TRAIL_DRAWDOWN_PCT) -> List[str]:
    """
    生成「模拟盘持仓」markdown 段（移动止盈结算 / 自动补仓结果）

    - holdings_status：当前活跃持仓的评估结果列表
    - closed_today：当日触发止损/移动止盈并已平仓的持仓
    - new_positions：当日自动补仓的新增持仓
    - trail_started：当日新启动移动止盈的持仓
    - stats：累计战绩（portfolio_stats() 的返回值）
    """
    rows = holdings_status or []
    closed_today = closed_today or []
    new_positions = new_positions or []
    trail_started = trail_started or []

    lines = [f'## 💼 模拟盘持仓（{len(rows)}/{max_holdings}）', '']

    if rows:
        lines.append('| # | 代码 | 名称 | 买入日 | 买入价 | 现价 | 盈亏 | 距止损 | 距止盈 | 状态 |')
        lines.append('|---|---|---|---|---|---|---|---|---|---|')
        for i, r in enumerate(rows, 1):
            trail_active = bool(r.get('trail_active'))
            if r.get('entry_pending'):
                status = '⏳ 待开盘价'
            elif trail_active and r.get('trail_high'):
                # 已启动移动止盈 → 状态里带上峰值，一眼看出锁利位置
                status = f"🔒 移动止盈(峰{r['trail_peak_pnl']:+.0f}%)"
            else:
                status = r.get('status_text') or '🟢 正常持有'
            bdate = format_md_date(r.get('entry_date') or r.get('buy_date'))
            # 距止盈：未启动＝距启动线；已启动＝距回撤触发线
            dist_tp = r.get('distance_to_take_profit')
            dist_txt = f'{dist_tp:+.1f}%' if dist_tp is not None else '-'
            lines.append(
                f"| {i} | {r['code']} | {r['name']} | {bdate} | "
                f"{r['buy_price']:.2f} | {r['current_price']:.2f} | "
                f"**{r['pnl_pct']:+.2f}%** | "
                f"{r['distance_to_stop_loss']:+.1f}% | {dist_txt} | {status} |"
            )
        lines.append('')
        lines.append(f'> 💡 买入价＝信号次日开盘价（模拟盘口径）｜止损 {stop_loss_pct}% ／ '
                     f'盈利 +{trail_activate_pct}% 启动**移动止盈**（峰值回撤 {trail_drawdown_pct}% 卖出）')
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

    # 今日触发（移动止盈 / 止损）
    if closed_today:
        lines.append('### 🚨 今日触发')
        lines.append('')
        for h in closed_today:
            reason = h.get('close_reason')
            peak = h.get('closed_peak_pnl')
            if reason == 'take_profit':
                icon, label = '🔒', '移动止盈卖出'
                if peak is not None:
                    label += f'（峰值 {peak:+.1f}%）'
            elif reason == 'stop_loss':
                icon, label = '🚨', '触发止损'
            else:
                icon, label = '⚠️', '已平仓'
            pnl = h.get('closed_pnl')
            pnl_txt = f'{pnl:+.2f}%' if pnl is not None else '-'
            price_txt = ''
            if h.get('closed_price'):
                price_txt = f'（{h.get("buy_price"):.2f} → {h["closed_price"]:.2f}）'
            lines.append(f'- {icon} **{h.get("name")}({h.get("code")})** {label} **{pnl_txt}**{price_txt} → 已平仓')
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
        lines.append('')

    if not rows and not closed_today and not new_positions:
        lines.append('> 📭 当前空仓，等待下一次选股/补仓。')
        lines.append('')

    return lines


def generate_review_payload(date_str: str, top_picks: pd.DataFrame,
                            warnings: pd.DataFrame, all_stocks: pd.DataFrame,
                            market: Dict[str, Any] = None,
                            sector_heat: List[Dict] = None,
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
    - 板块温度 TOP3（按题材聚合力强板块）
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
        lines.append('| # | 代码 | 名称 | 现价 | 当日 | 主力净额 | 评分 | 关键 |')
        lines.append('|---|---|---|---|---|---|---|---|')
        for i, (_, row) in enumerate(top_picks.iterrows(), 1):
            medal = ['🥇', '🥈', '🥉'][i - 1] if i <= 3 else str(i)
            inflow = format_yi(row.get('main_net_inflow', 0))
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

    # ---- 板块温度 TOP3 ----
    if sector_heat:
        lines.append('## 🔥 板块温度 TOP3')
        lines.append('')
        for i, sector in enumerate(sector_heat, 1):
            medal = ['🥇', '🥈', '🥉'][i - 1] if i <= 3 else str(i)
            emoji_up = '🟢' if sector['avg_pct'] >= 0 else '🔴'
            lines.append(
                f'- {medal} **{sector["theme"]}** - {emoji_up}平均涨幅 {sector["avg_pct"]:+.1f}% ｜ '
                f'主力 {sector["inflow_yi"]:+.1f}亿 ｜ 涨停 {sector["limit_up"]}只'
            )
        lines.append('')

    # ---- 明日展望 ----
    lines.append('## 💡 明日展望')
    lines.append('')
    outlook = generate_outlook(market or {}, sector_heat or [])
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
    lines.append(f'> 筛选：沪深主板 / 非ST / 趋势向上 / 主力流入')
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
    lines.append('| 技术面 | 25% | 当日涨幅、5日涨幅、量比 |')
    lines.append('| 资金面 | 20% | 主力净流入、换手率 |')
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
            lines.append(f'- 主力净流入：{format_yi(row.get("main_net_inflow", 0))}')
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
        main_inflow_total = all_stocks['main_net_inflow'].sum() / 1e8
        lines.append(f'- **主力净流入合计**：{main_inflow_total:+.1f}亿')
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
            lines.append(f'- 主力净流入：{format_yi(row.get("main_net_inflow", 0))}')
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

    # 板块温度
    if sector_heat:
        lines.append('## 🔥 板块温度 TOP3')
        lines.append('')
        for i, sector in enumerate(sector_heat, 1):
            medal = ['🥇', '🥈', '🥉'][i - 1] if i <= 3 else str(i)
            lines.append(
                f'- {medal} **{sector["theme"]}** - 平均涨幅 {sector["avg_pct"]:+.1f}% ｜ '
                f'主力 {sector["inflow_yi"]:+.1f}亿 ｜ 涨停 {sector["limit_up"]}只'
            )
        lines.append('')

    # 明日展望
    lines.append('## 💡 明日展望')
    lines.append('')
    outlook = generate_outlook(market or {}, sector_heat or [])
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