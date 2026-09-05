"""
报告生成模块 - 输出 Markdown 报告 + Server酱推送内容
====================================================
Server酱支持 Markdown 格式（与钉钉类似），但有些语法差异：
- 不支持 :::tip 等扩展语法
- 表格语法相同
- emoji 相同
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
    lines.append(f'> 📡 数据源：akshare')
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

    # TOP 表格
    lines.append(f'## 📋 TOP {len(top_picks)} 精选')
    lines.append('')
    if not top_picks.empty:
        lines.append('| # | 代码 | 名称 | 现价 | 当日 | 主力净额 | 评分 | 关键 |')
        lines.append('|---|---|---|---|---|---|---|---|')
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
                f'{inflow} | {score} | {catalyst} |'
            )
        lines.append('')

    # 操作指引
    if not top_picks.empty:
        lines.append('## ⚡ 操作指引')
        lines.append('')
        for i, (_, row) in enumerate(top_picks.iterrows(), 1):
            medal = ['🥇', '🥈', '🥉'][i - 1] if i <= 3 else f'{i}'
            lines.append(
                f'**{medal} {row["name"]}** - 关注开盘强弱，回调时小仓试探。'
                f'PE {row.get("pe_ttm", "-"):.0f}，'
                f'主力 {format_yi(row.get("main_net_inflow", 0))}'
            )
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
    lines.append('⚠️ **免责声明**：以上内容由 AI 基于 akshare 公开数据生成，'
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
    lines.append(f'> 数据时间：{datetime.now().strftime("%Y-%m-%d %H:%M:%S")} · 数据源：akshare')
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
    lines.append('⚠️ **免责声明**：以上内容由 AI 基于 akshare 公开数据自动生成，仅供参考，'
                 '不构成任何投资建议。投资有风险，决策需谨慎。')

    content = '\n'.join(lines)

    # 保存
    report_path = reports_dir / f'主升浪精选_{date_str}.md'
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(content)

    return str(report_path)


if __name__ == '__main__':
    print('报告生成模块独立测试需要 main.py 配合')