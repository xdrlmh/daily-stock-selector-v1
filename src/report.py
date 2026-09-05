"""
报告生成模块 - 输出 Markdown 报告 + 钉钉推送内容
==================================================
- 钉钉机器人 markdown 子集支持：标题、加粗、引用、列表、表格、emoji
- 不支持 :::tip 等扩展语法
"""
import json
from datetime import datetime
from typing import Dict, List, Tuple
import pandas as pd
from pathlib import Path


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


def generate_dingtalk_payload(date_str: str, top_picks: pd.DataFrame,
                                warnings: pd.DataFrame, all_stocks: pd.DataFrame) -> Dict:
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
            score = f'{row["total_score"]:.0f}'
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


def save_full_report(date_str: str, top_picks: pd.DataFrame,
                    warnings: pd.DataFrame, all_stocks: pd.DataFrame,
                    reports_dir: Path) -> str:
    """
    生成完整版 Markdown 报告（保存到 reports/ 目录做历史记录）
    """
    lines = []
    lines.append(f'# 🎯 主升浪精选日报 {date_str}')
    lines.append('')
    lines.append(f'> 数据时间：{datetime.now().strftime("%Y-%m-%d %H:%M:%S")} · 数据源：Tushare')
    lines.append(f'> 筛选：沪深主板 / 非ST / 趋势向上 / 主力流入')
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
            lines.append(f'- 换手率：{row.get("turnover_rate", 0):.2f}%')
            lines.append(f'- 量比：{row.get("volume_ratio", 0):.2f}')
            lines.append(f'- PE-TTM：{row.get("pe_ttm", "-"):.1f}')
            lines.append(f'- 流通市值：{row.get("circ_mcap", 0)/1e8:.1f}亿')
            lines.append(f'- 主力净流入：{format_yi(row.get("main_net_inflow", 0))}')
            lines.append('')
            lines.append(f'**综合评分：{row["total_score"]:.0f}/100**')
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


if __name__ == '__main__':
    print('报告生成模块独立测试需要 main.py 配合')