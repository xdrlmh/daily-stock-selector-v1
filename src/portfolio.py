"""
持仓池管理模块（模拟盘）
==========================
设计要点：
1. 最多同时持有 MAX_HOLDINGS(3) 只；
2. 买入价按「信号日次日开盘价」归一化（贴近模拟盘真实买入成本）；
3. 止盈/止损平仓后自动补仓（由盘后复盘流程调用，详见 main_review.py）；
4. 持仓池持久化到 data/portfolio.json（GitHub Actions 中由 workflow 回写仓库）。

数据模型（version 2）：
{
  "version": 2,
  "updated_at": "2026-09-11 18:30:00",
  "holdings": [
    {
      "code": "601975", "name": "招商南油",
      "buy_price": 4.48,          # entry_pending=true 时为信号日收盘价
      "buy_date": "2026-09-12",   # 信号日
      "entry_date": null,         # 实际建仓日（=信号次日，归一化后写入）
      "entry_pending": true,      # true=等待次日开盘价归一化
      "shares": 0,
      "stop_loss_pct": -7.0,
      "take_profit_pct": 15.0,
      "alerted": false, "alerted_at": null,
      "closed": false, "closed_at": null,
      "closed_price": null, "closed_pnl": null, "close_reason": null,
      "source": "morning_screen"  # morning_screen | review_refill
    }
  ]
}
"""
import json
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Optional, Any

# ============= 路径配置 =============
PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / 'data'
PORTFOLIO_PATH = DATA_DIR / 'portfolio.json'

# ============= 持仓参数 =============
DEFAULT_STOP_LOSS_PCT = -7.0     # 默认止损 -7%
DEFAULT_TAKE_PROFIT_PCT = 15.0   # 默认止盈 +15%
WARNING_PROFIT_PCT = 10.0        # 接近止盈的提醒阈值
MAX_HOLDINGS = 3                 # 最大同时持仓数（模拟盘买进头 3 只）
MAX_CLOSED_KEEP = 30             # 仅保留最近 N 条已平仓记录，防止文件无限膨胀

STATUS_TEXT = {
    'stop_loss': '🚨 触发止损',
    'take_profit': '🎯 触发止盈',
    'warning': '⚡ 接近止盈',
    'normal': '🟢 正常持有',
    'pending': '⏳ 待开盘价',
    'no_quote': '❓ 无行情',
}


# ============= 基础读写 =============
def _ensure_data_dir():
    DATA_DIR.mkdir(exist_ok=True)


def _empty_portfolio() -> Dict:
    return {
        'version': 2,
        'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'holdings': [],
    }


def load_portfolio() -> Dict:
    """加载持仓池；文件不存在或损坏时返回空持仓池"""
    _ensure_data_dir()
    if not PORTFOLIO_PATH.exists():
        return _empty_portfolio()
    try:
        with open(PORTFOLIO_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict) or 'holdings' not in data:
            return _empty_portfolio()
        return data
    except (json.JSONDecodeError, IOError) as e:
        print(f'⚠️ 读取持仓池失败: {e}，返回空持仓池')
        return _empty_portfolio()


def save_portfolio(portfolio: Dict) -> None:
    """保存持仓池到本地文件"""
    _ensure_data_dir()
    portfolio['version'] = 2
    portfolio['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with open(PORTFOLIO_PATH, 'w', encoding='utf-8') as f:
        json.dump(portfolio, f, ensure_ascii=False, indent=2)
    active_n = len([h for h in portfolio.get('holdings', []) if not h.get('closed')])
    print(f'✅ 持仓池已保存: {PORTFOLIO_PATH} '
          f'(活跃 {active_n} / 总记录 {len(portfolio.get("holdings", []))})')


def _fmt_date(date_str: str) -> str:
    """YYYYMMDD → YYYY-MM-DD（已是带横杠格式则原样返回）"""
    if not date_str:
        return datetime.now().strftime('%Y-%m-%d')
    s = str(date_str)
    if len(s) == 8 and s.isdigit():
        return f'{s[:4]}-{s[4:6]}-{s[6:]}'
    return s


def _prune_closed(portfolio: Dict) -> None:
    """只保留最近 MAX_CLOSED_KEEP 条已平仓记录"""
    holdings = portfolio.get('holdings', [])
    active = [h for h in holdings if not h.get('closed')]
    closed = [h for h in holdings if h.get('closed')]
    closed.sort(key=lambda x: x.get('closed_at') or '', reverse=True)
    portfolio['holdings'] = active + closed[:MAX_CLOSED_KEEP]


# ============= 查询 =============
def get_active_holdings() -> List[Dict]:
    """所有未平仓的活跃持仓"""
    return [h for h in load_portfolio().get('holdings', []) if not h.get('closed', False)]


def get_active_codes() -> List[str]:
    return [h['code'] for h in get_active_holdings()]


def available_slots() -> int:
    """当前还剩几个可买入仓位"""
    return max(0, MAX_HOLDINGS - len(get_active_holdings()))


def get_recent_closed_holdings(limit: int = 5) -> List[Dict]:
    """最近平仓的持仓（按平仓时间倒序）"""
    closed = [h for h in load_portfolio().get('holdings', []) if h.get('closed', False)]
    closed.sort(key=lambda x: x.get('closed_at') or '', reverse=True)
    return closed[:limit]


def get_closed_on(date_str: str) -> List[Dict]:
    """指定日期（YYYY-MM-DD）平仓的持仓"""
    day = _fmt_date(date_str)
    closed = [h for h in load_portfolio().get('holdings', [])
              if h.get('closed', False) and str(h.get('closed_at') or '').startswith(day)]
    closed.sort(key=lambda x: x.get('closed_at') or '')
    return closed


def portfolio_stats() -> Dict[str, Any]:
    """模拟盘累计战绩（基于已平仓记录）"""
    closed = [h for h in load_portfolio().get('holdings', [])
              if h.get('closed', False) and h.get('closed_pnl') is not None]
    if not closed:
        return {'total': 0, 'wins': 0, 'losses': 0, 'win_rate': None, 'avg_pnl': None}
    wins = [h for h in closed if h['closed_pnl'] > 0]
    losses = [h for h in closed if h['closed_pnl'] <= 0]
    return {
        'total': len(closed),
        'wins': len(wins),
        'losses': len(losses),
        'win_rate': round(len(wins) / len(closed) * 100, 1),
        'avg_pnl': round(sum(h['closed_pnl'] for h in closed) / len(closed), 2),
    }


# ============= 评估 =============
def _hold_days(holding: Dict) -> Optional[int]:
    base = holding.get('entry_date') or holding.get('buy_date')
    if not base:
        return None
    try:
        d0 = datetime.strptime(_fmt_date(base), '%Y-%m-%d')
        return (datetime.now() - d0).days
    except ValueError:
        return None


def evaluate_holding(holding: Dict, current_price: float) -> Optional[Dict]:
    """
    评估单只持仓（纯计算，不落盘）。

    触发规则：
    - 止损：盈亏 ≤ stop_loss_pct（默认 -7%）
    - 止盈：盈亏 ≥ take_profit_pct（默认 +15%）
    - 警戒：盈亏 ≥ WARNING_PROFIT_PCT（默认 +10%），且尚未提醒过
    """
    buy = float(holding.get('buy_price') or 0)
    if buy <= 0 or current_price is None or float(current_price) <= 0:
        return None

    current_price = float(current_price)
    pnl_pct = round((current_price / buy - 1) * 100, 2)
    stop = float(holding.get('stop_loss_pct', DEFAULT_STOP_LOSS_PCT))
    take = float(holding.get('take_profit_pct', DEFAULT_TAKE_PROFIT_PCT))

    if pnl_pct <= stop:
        trigger = 'stop_loss'
    elif pnl_pct >= take:
        trigger = 'take_profit'
    elif pnl_pct >= WARNING_PROFIT_PCT and not holding.get('alerted'):
        trigger = 'warning'
    else:
        trigger = 'normal'

    return {
        'code': holding['code'],
        'name': holding.get('name', ''),
        'buy_price': round(buy, 3),
        'current_price': round(current_price, 3),
        'pnl_pct': pnl_pct,
        'trigger': trigger,
        'status_text': STATUS_TEXT.get(trigger, ''),
        'distance_to_stop_loss': round(pnl_pct - stop, 2),
        'distance_to_take_profit': round(take - pnl_pct, 2),
        'hold_days': _hold_days(holding),
        'entry_pending': bool(holding.get('entry_pending')),
    }


# ============= 写入 / 变更 =============
def add_to_portfolio(code: str, name: str, buy_price: float,
                     buy_date: Optional[str] = None,
                     stop_loss_pct: float = DEFAULT_STOP_LOSS_PCT,
                     take_profit_pct: float = DEFAULT_TAKE_PROFIT_PCT,
                     source: str = 'morning_screen') -> Optional[Dict]:
    """
    新增一只持仓。
    - 已存在（任意未平仓）→ 跳过
    - 仓位已满（>= MAX_HOLDINGS）→ 跳过
    - entry_pending=True：buy_price 为信号日收盘价，待次日开盘归一化
    返回新增的 holding（未新增则 None）
    """
    code = str(code).zfill(6)
    portfolio = load_portfolio()
    holdings = portfolio.get('holdings', [])

    if any(h['code'] == code and not h.get('closed') for h in holdings):
        print(f'  ⏭️ {name}({code}) 已在持仓池，跳过')
        return None

    active = [h for h in holdings if not h.get('closed')]
    if len(active) >= MAX_HOLDINGS:
        print(f'  ⚠️ 持仓已满（{MAX_HOLDINGS} 只），跳过 {name}({code})')
        return None

    holding = {
        'code': code,
        'name': name,
        'buy_price': round(float(buy_price), 3),
        'buy_date': _fmt_date(buy_date or datetime.now().strftime('%Y-%m-%d')),
        'entry_date': None,
        'entry_pending': True,
        'shares': 0,
        'stop_loss_pct': float(stop_loss_pct),
        'take_profit_pct': float(take_profit_pct),
        'alerted': False,
        'alerted_at': None,
        'closed': False,
        'closed_at': None,
        'closed_price': None,
        'closed_pnl': None,
        'close_reason': None,
        'source': source,
    }
    holdings.append(holding)
    save_portfolio(portfolio)
    print(f'  ➕ 加入持仓池: {name}({code}) 参考价 {buy_price:.2f} '
          f'止损{stop_loss_pct}% / 止盈{take_profit_pct}%（来源：{source}）')
    return holding


def fill_portfolio_from_candidates(candidates_df, exclude_codes: Optional[List[str]] = None,
                                   source: str = 'morning_screen',
                                   max_add: Optional[int] = None) -> List[Dict]:
    """
    从候选（按评分排序的 DataFrame，需含 code/name/price 列）依次补仓，
    直到持仓数达到 MAX_HOLDINGS（或候选耗尽）。
    - exclude_codes：排除名单（如当日刚平仓、避免同日回补）
    - max_add：本次最多新增数量（默认补齐全部空仓位）
    返回新增的 holding 列表。
    """
    if candidates_df is None or getattr(candidates_df, 'empty', True):
        return []

    slots = available_slots()
    if max_add is not None:
        slots = min(slots, max_add)
    if slots <= 0:
        print(f'  ⏭️ 持仓已满（{MAX_HOLDINGS} 只），本次不补仓')
        return []

    exclude = {str(c).zfill(6) for c in (exclude_codes or [])}
    active = set(get_active_codes())

    added: List[Dict] = []
    for _, row in candidates_df.iterrows():
        if len(added) >= slots:
            break
        code = str(row.get('code', '')).zfill(6)
        if not code or code in active or code in exclude:
            continue
        price = row.get('price', row.get('close', 0))
        try:
            price = float(price)
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        h = add_to_portfolio(code=code, name=str(row.get('name', '')),
                             buy_price=price, source=source)
        if h:
            added.append(h)
            active.add(code)

    return added


def normalize_pending_entries(price_map: Dict[str, Dict], data_date: str) -> List[Dict]:
    """
    把「待定入场价」的持仓按当日开盘价归一化（贴近模拟盘实际买入成本）。
    price_map: {code: {'open': float, 'price': float, ...}}
    data_date: 行情日期（YYYYMMDD 或 YYYY-MM-DD）
    返回被归一化的持仓列表（含 old/new 价格）。
    """
    date_str = _fmt_date(data_date)
    portfolio = load_portfolio()
    changed: List[Dict] = []
    for h in portfolio.get('holdings', []):
        if h.get('closed') or not h.get('entry_pending'):
            continue
        info = price_map.get(h['code'])
        if not info:
            continue
        try:
            open_px = float(info.get('open') or 0)
        except (TypeError, ValueError):
            open_px = 0
        if open_px <= 0:
            continue
        old = h.get('buy_price')
        h['buy_price'] = round(open_px, 3)
        h['entry_pending'] = False
        h['entry_date'] = date_str
        changed.append({'code': h['code'], 'name': h.get('name', ''),
                        'old': old, 'new': h['buy_price'], 'entry_date': date_str})

    if changed:
        save_portfolio(portfolio)
        for c in changed:
            print(f'  🔧 归一化入场价: {c["name"]}({c["code"]}) '
                  f'{c["old"]} → {c["new"]}（{c["entry_date"]} 开盘）')
    return changed


def close_holding(code: str, pnl_pct: float, reason: str = 'stop_loss',
                  price: Optional[float] = None) -> Optional[Dict]:
    """标记某只持仓已止盈/止损出场。返回被平仓的 holding（未找到则 None）"""
    code = str(code).zfill(6)
    portfolio = load_portfolio()
    target = None
    for h in portfolio.get('holdings', []):
        if h['code'] == code and not h.get('closed'):
            h['closed'] = True
            h['closed_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            h['closed_price'] = round(float(price), 3) if price else None
            h['closed_pnl'] = round(float(pnl_pct), 2)
            h['close_reason'] = reason
            target = h
            break
    if target:
        _prune_closed(portfolio)
        save_portfolio(portfolio)
    return target


def mark_alerted(code: str) -> None:
    """标记某只持仓已推送过「接近止盈」提醒（避免重复提醒）"""
    code = str(code).zfill(6)
    portfolio = load_portfolio()
    for h in portfolio.get('holdings', []):
        if h['code'] == code and not h.get('closed'):
            h['alerted'] = True
            h['alerted_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            break
    save_portfolio(portfolio)


def clear_portfolio() -> None:
    """清空持仓池（仅供测试用）"""
    save_portfolio(_empty_portfolio())


# ============= CLI 测试入口 =============
if __name__ == '__main__':
    import sys
    arg = sys.argv[1] if len(sys.argv) > 1 else 'show'
    if arg == 'show':
        portfolio = load_portfolio()
        active = get_active_holdings()
        print(f'\n📂 当前持仓池（活跃 {len(active)}/{MAX_HOLDINGS}）：')
        for h in active:
            flag = '⏳待归一化' if h.get('entry_pending') else '🟢已建仓'
            print(f"  {flag} {h['name']}({h['code']}) @ {h['buy_price']} "
                  f"止损{h['stop_loss_pct']}% / 止盈{h['take_profit_pct']}% 来源={h.get('source')}")
        recent = get_recent_closed_holdings(5)
        if recent:
            print(f'\n📜 最近平仓（{len(recent)} 条）：')
            for h in recent:
                print(f"  [{h.get('close_reason')}] {h['name']}({h['code']}) "
                      f"{h.get('closed_pnl'):+.2f}% @ {h.get('closed_at')}")
        stats = portfolio_stats()
        if stats['total']:
            print(f"\n📊 累计战绩：{stats['total']} 笔，胜率 {stats['win_rate']}%，"
                  f"平均 {stats['avg_pnl']:+.2f}%")
    elif arg == 'clear':
        clear_portfolio()
        print('🧹 持仓池已清空')
    else:
        print('用法: python src/portfolio.py [show|clear]')
