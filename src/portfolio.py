"""
持仓池管理模块
==========================
- 持仓池持久化到 data/portfolio.json
- GitHub Actions 环境下：读写需要通过 GitHub Contents API 提交回仓库
- 本地测试环境下：直接读写本地文件
"""
import os
import json
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Optional

# ============= 路径配置 =============
PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / 'data'
PORTFOLIO_PATH = DATA_DIR / 'portfolio.json'

# ============= 默认持仓默认值 =============
DEFAULT_STOP_LOSS_PCT = -7.0   # 默认止损 -7%
DEFAULT_TAKE_PROFIT_PCT = 15.0  # 默认止盈 +15%
MAX_HOLDINGS = 10                # 最多持仓 10 只（防止 portfolio 无限增长）


def _ensure_data_dir():
    """确保 data 目录存在"""
    DATA_DIR.mkdir(exist_ok=True)


def _empty_portfolio() -> Dict:
    """返回空持仓池结构"""
    return {
        'version': 1,
        'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'holdings': []
    }


def load_portfolio() -> Dict:
    """
    加载持仓池。
    如果文件不存在，返回空持仓池。
    """
    _ensure_data_dir()
    if not PORTFOLIO_PATH.exists():
        return _empty_portfolio()
    try:
        with open(PORTFOLIO_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print(f'⚠️ 读取持仓池失败: {e}，返回空持仓池')
        return _empty_portfolio()


def save_portfolio(portfolio: Dict) -> None:
    """保存持仓池到本地文件"""
    _ensure_data_dir()
    portfolio['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with open(PORTFOLIO_PATH, 'w', encoding='utf-8') as f:
        json.dump(portfolio, f, ensure_ascii=False, indent=2)
    print(f'✅ 持仓池已保存: {PORTFOLIO_PATH} ({len(portfolio.get("holdings", []))} 只持仓)')


def _is_in_holdings(holdings: List[Dict], code: str) -> bool:
    """判断某只票是否已在持仓中"""
    return any(h['code'] == code for h in holdings)


def add_to_portfolio(
    code: str,
    name: str,
    buy_price: float,
    buy_date: Optional[str] = None,
    stop_loss_pct: float = DEFAULT_STOP_LOSS_PCT,
    take_profit_pct: float = DEFAULT_TAKE_PROFIT_PCT,
) -> Dict:
    """
    添加持仓到持仓池。
    - 如果持仓池已满（>= MAX_HOLDINGS），跳过
    - 如果该 code 已存在，跳过（不重复添加）
    - buy_date 默认为今天
    返回新增的 holding dict（已存在或池满则返回 None）
    """
    if buy_date is None:
        buy_date = datetime.now().strftime('%Y-%m-%d')

    portfolio = load_portfolio()
    holdings = portfolio.get('holdings', [])

    if _is_in_holdings(holdings, code):
        print(f'  ⏭️ {name}({code}) 已在持仓池，跳过')
        return None

    if len(holdings) >= MAX_HOLDINGS:
        print(f'  ⚠️ 持仓池已满({MAX_HOLDINGS}只)，跳过 {name}({code})')
        return None

    holding = {
        'code': code,
        'name': name,
        'buy_price': round(float(buy_price), 3),
        'buy_date': buy_date,
        'shares': 0,  # 用户模拟盘手动记录，不强制填
        'stop_loss_pct': float(stop_loss_pct),
        'take_profit_pct': float(take_profit_pct),
        'alerted': False,
        'alerted_at': None,
        'closed': False,        # 是否已止盈/止损出场
        'closed_at': None,
        'closed_pnl': None,     # 出场时盈亏 %
    }
    holdings.append(holding)
    save_portfolio(portfolio)
    print(f'  ➕ 已加入持仓池: {name}({code}) @ {buy_price:.2f} 止损{stop_loss_pct}% / 止盈{take_profit_pct}%')
    return holding


def add_top3_from_screening(candidates_df) -> int:
    """
    把选股结果的 TOP 3 写入持仓池。
    candidates_df 必须是包含 code/name/close 列的 DataFrame。
    返回成功添加的数量。
    """
    if candidates_df is None or candidates_df.empty:
        return 0
    added = 0
    for _, row in candidates_df.head(3).iterrows():
        result = add_to_portfolio(
            code=row['code'],
            name=row['name'],
            buy_price=row.get('close', 0),
        )
        if result:
            added += 1
    return added


def update_holding_alerted(code: str) -> None:
    """标记某只持仓已推送过告警（避免重复告警）"""
    portfolio = load_portfolio()
    for h in portfolio.get('holdings', []):
        if h['code'] == code and not h['closed']:
            h['alerted'] = True
            h['alerted_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            break
    save_portfolio(portfolio)


def close_holding(code: str, pnl_pct: float) -> None:
    """标记某只持仓已止盈/止损出场"""
    portfolio = load_portfolio()
    for h in portfolio.get('holdings', []):
        if h['code'] == code and not h['closed']:
            h['closed'] = True
            h['closed_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            h['closed_pnl'] = round(float(pnl_pct), 2)
            break
    save_portfolio(portfolio)


def get_active_holdings() -> List[Dict]:
    """获取所有未平仓的活跃持仓"""
    portfolio = load_portfolio()
    return [h for h in portfolio.get('holdings', []) if not h.get('closed', False)]


def get_recent_closed_holdings(days: int = 5) -> List[Dict]:
    """获取最近 N 天内平仓的持仓（用于盘后复盘回顾）"""
    portfolio = load_portfolio()
    closed = [h for h in portfolio.get('holdings', []) if h.get('closed', False)]
    # 按 closed_at 倒序
    closed.sort(key=lambda x: x.get('closed_at', ''), reverse=True)
    return closed[:days]


def clear_portfolio() -> None:
    """清空持仓池（仅供测试用）"""
    save_portfolio(_empty_portfolio())


# ============= CLI 测试入口 =============
if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == 'show':
        portfolio = load_portfolio()
        print(f'\n📂 当前持仓池 ({len(portfolio["holdings"])} 只):')
        for h in portfolio['holdings']:
            status = '✅ 已平仓' if h.get('closed') else ('🔔 已告警' if h.get('alerted') else '🟢 监控中')
            print(f"  {status} {h['name']}({h['code']}) @ {h['buy_price']} 止损{h['stop_loss_pct']}% / 止盈{h['take_profit_pct']}%")
    elif len(sys.argv) > 1 and sys.argv[1] == 'clear':
        clear_portfolio()
        print('🧹 持仓池已清空')
    else:
        print('用法: python src/portfolio.py [show|clear]')