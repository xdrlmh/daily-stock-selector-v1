"""
持仓池管理模块（模拟盘）
==========================
设计要点：
1. 最多同时持有 MAX_HOLDINGS(3) 只；
2. 买入价按「信号日次日开盘价」归一化（贴近模拟盘真实买入成本）；
3. 止盈＝**移动止盈**：盈利首次达到 +15% 后启动，随后跟踪启动以来的最高价，
   从最高价回撤 8% 即卖出（保护利润，同时给主升浪留足呼吸空间）；
4. 平仓后自动补仓（由盘后复盘流程调用，详见 main_review.py）；
5. 持仓池持久化到 data/portfolio.json（GitHub Actions 中由 workflow 回写仓库）。

移动止盈细则：
- 启动线：盈利 ≥ trail_activate_pct（默认 +15%）
- 峰值 trail_high：取「盘中最高价」与收盘价中的较大者，逐日抬升（只上不下）
- 触发线：trail_high × (1 - trail_drawdown_pct/100)，默认回撤 8%
- 判定：盘中最低价 ≤ 触发线 → 视为触发，成交价＝触发线（贴近真实移动止盈单）
- 止损优先：收盘盈亏 ≤ stop_loss_pct（默认 -7%）时直接止损，不再看移动止盈

数据模型（version 2）：
{
  "version": 2,
  "updated_at": "2026-09-11 18:30:00",
  "holdings": [
    {
      "code": "601975", "name": "招商南油",
      "buy_price": 4.48,               # entry_pending=true 时为信号日收盘价
      "buy_date": "2026-09-12",        # 信号日
      "entry_date": null,              # 实际建仓日（=信号次日，归一化后写入）
      "entry_pending": true,           # true=等待次日开盘价归一化
      "shares": 0,
      "stop_loss_pct": -7.0,           # 固定止损
      "trail_activate_pct": 15.0,      # 移动止盈启动线
      "trail_drawdown_pct": 8.0,       # 启动后回撤卖出幅度
      "trail_active": false,           # 移动止盈是否已启动
      "trail_high": null,              # 启动以来的最高价（只上不下）
      "alerted": false, "alerted_at": null,
      "closed": false, "closed_at": null,
      "closed_price": null, "closed_pnl": null, "close_reason": null,
      "closed_peak_pnl": null,         # 平仓时的峰值盈亏（复盘展示用）
      "source": "morning_screen"       # morning_screen | review_refill
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
DEFAULT_STOP_LOSS_PCT = -7.0     # 固定止损 -7%
TRAIL_ACTIVATE_PCT = 15.0        # 盈利达到 +15% 启动移动止盈
TRAIL_DRAWDOWN_PCT = 8.0         # 启动后从最高价回撤 8% → 卖出（3=激进锁利 / 8=宽吃趋势）
WARNING_PROFIT_PCT = 10.0        # 接近启动线的提醒阈值
DEFAULT_TAKE_PROFIT_PCT = TRAIL_ACTIVATE_PCT   # 兼容旧字段/旧调用（含义＝启动线）
MAX_HOLDINGS = 3                 # 最大同时持仓数（模拟盘买进头 3 只）
MAX_CLOSED_KEEP = 30             # 仅保留最近 N 条已平仓记录，防止文件无限膨胀

STATUS_TEXT = {
    'stop_loss': '🚨 触发止损',
    'take_profit': '🔒 移动止盈卖出',
    'trailing': '🔒 移动止盈中',
    'warning': '⚡ 接近启动线',
    'normal': '🟢 正常持有',
    'pending': '⏳ 待开盘价',
    'no_quote': '❓ 无行情',
}


# ============= 内部工具 =============
def _to_float(value: Any) -> float:
    try:
        if value is None:
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _pnl_pct(price: float, buy: float) -> float:
    """价格相对买入价的盈亏百分比（保留 2 位，规避浮点误差如 11.5/10→14.9999…）"""
    if buy <= 0:
        return 0.0
    return round((price / buy - 1) * 100, 2)


def _ensure_data_dir():
    DATA_DIR.mkdir(exist_ok=True)


def _empty_portfolio() -> Dict:
    return {
        'version': 2,
        'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'holdings': [],
    }


def _migrate(portfolio: Dict) -> Dict:
    """老版本持仓补齐移动止盈字段（幂等）"""
    for h in portfolio.get('holdings', []):
        h.setdefault('stop_loss_pct', DEFAULT_STOP_LOSS_PCT)
        if 'trail_activate_pct' not in h:
            h['trail_activate_pct'] = _to_float(h.get('take_profit_pct')) or TRAIL_ACTIVATE_PCT
        h.setdefault('trail_drawdown_pct', TRAIL_DRAWDOWN_PCT)
        h.setdefault('trail_active', False)
        h.setdefault('trail_high', None)
        h.setdefault('closed_peak_pnl', None)
        # 兼容旧读者：take_profit_pct 始终＝启动线
        h['take_profit_pct'] = h['trail_activate_pct']
    return portfolio


# ============= 基础读写 =============
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
        return _migrate(data)
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


def evaluate_holding(holding: Dict, current_price: float,
                     day_high: Optional[float] = None,
                     day_low: Optional[float] = None) -> Optional[Dict]:
    """
    评估单只持仓（纯计算，不落盘）。

    触发规则：
    - 止损：收盘盈亏 ≤ stop_loss_pct（默认 -7%）——优先级最高
    - 移动止盈：
        1) 盈利（按盘中最高价）首次 ≥ trail_activate_pct（默认 +15%）→ 启动
        2) 启动后 trail_high 取「盘中最高价 / 收盘价」的最大值（只上不下）
        3) 触发线 = trail_high × (1 - trail_drawdown_pct/100)
        4) 盘中最低价 ≤ 触发线 → 卖出，成交价＝触发线
    - 警戒：盈利 ≥ WARNING_PROFIT_PCT（默认 +10%）且尚未启动，一次性提醒

    day_high / day_low：当日盘中最高/最低价（缺省时退化为用收盘价判断）

    返回 dict；无有效行情时返回 None。
    额外返回（供调用方落盘）：
      trail_active / trail_high / trail_started（本次是否新启动）
      exit_price / exit_pnl_pct（触发卖出时的成交价与盈亏）
    """
    buy = _to_float(holding.get('buy_price'))
    if buy <= 0 or current_price is None or _to_float(current_price) <= 0:
        return None

    current_price = _to_float(current_price)
    high = _to_float(day_high) or current_price
    low = _to_float(day_low) or current_price
    high = max(high, current_price)
    low = min(low, current_price) if low > 0 else current_price

    pnl_pct = _pnl_pct(current_price, buy)
    stop = _to_float(holding.get('stop_loss_pct')) or DEFAULT_STOP_LOSS_PCT
    activate = (_to_float(holding.get('trail_activate_pct'))
                or _to_float(holding.get('take_profit_pct'))
                or TRAIL_ACTIVATE_PCT)
    drawdown = _to_float(holding.get('trail_drawdown_pct')) or TRAIL_DRAWDOWN_PCT

    was_active = bool(holding.get('trail_active'))
    trail_active = was_active
    trail_high = _to_float(holding.get('trail_high')) or 0.0
    trail_started = False
    exit_price: Optional[float] = None
    exit_pnl_pct: Optional[float] = None

    # ---- 1) 止损优先 ----
    if pnl_pct <= stop:
        trigger = 'stop_loss'
        exit_price = round(current_price, 3)
        exit_pnl_pct = pnl_pct
        trail_active = was_active          # 止损不动移动止盈状态
        trail_high = trail_high or None

    else:
        # ---- 2) 更新峰值 / 判断启动 ----
        if not trail_active:
            peak_pnl = _pnl_pct(max(high, current_price), buy)
            if peak_pnl >= activate:
                trail_active = True
                trail_started = True
                trail_high = max(high, current_price)
        else:
            trail_high = max(trail_high, high, current_price)

        if trail_active:
            trigger_price = round(trail_high * (1 - drawdown / 100), 3)
            if low <= trigger_price:
                # 盘中触及回撤线 → 按触发价成交（移动止盈单口径）
                trigger = 'take_profit'
                exit_price = trigger_price
                exit_pnl_pct = _pnl_pct(trigger_price, buy)
            elif current_price <= trigger_price:
                # 收盘已跌破触发线（保护性兜底）
                trigger = 'take_profit'
                exit_price = round(current_price, 3)
                exit_pnl_pct = pnl_pct
            else:
                trigger = 'trailing'
        elif pnl_pct >= WARNING_PROFIT_PCT and not holding.get('alerted'):
            trigger = 'warning'
        else:
            trigger = 'normal'

    # ---- 3) 汇总 ----
    if trail_active and trail_high:
        trail_trigger_price = round(trail_high * (1 - drawdown / 100), 3)
        trail_peak_pnl = _pnl_pct(trail_high, buy)
        distance_to_trail = round((current_price / trail_trigger_price - 1) * 100, 2)
        distance_to_take_profit = distance_to_trail      # 已启动 → 距回撤线
    else:
        trail_trigger_price = None
        trail_peak_pnl = None
        distance_to_trail = None
        distance_to_take_profit = round(activate - pnl_pct, 2)   # 未启动 → 距启动线

    return {
        'code': holding['code'],
        'name': holding.get('name', ''),
        'buy_price': round(buy, 3),
        'buy_date': holding.get('buy_date'),
        'entry_date': holding.get('entry_date'),
        'current_price': round(current_price, 3),
        'day_high': round(high, 3),
        'day_low': round(low, 3),
        'pnl_pct': pnl_pct,
        'trigger': trigger,
        'status_text': STATUS_TEXT.get(trigger, ''),
        # 止损
        'stop_loss_pct': stop,
        'distance_to_stop_loss': round(pnl_pct - stop, 2),
        # 移动止盈
        'trail_activate_pct': activate,
        'trail_drawdown_pct': drawdown,
        'trail_active': trail_active,
        'trail_high': round(trail_high, 3) if trail_high else None,
        'trail_trigger_price': trail_trigger_price,
        'trail_peak_pnl': trail_peak_pnl,
        'trail_started': trail_started,
        'distance_to_trail': distance_to_trail,
        # 兼容旧字段：未启动＝距启动线；已启动＝距回撤线
        'distance_to_take_profit': distance_to_take_profit,
        # 平仓口径
        'exit_price': exit_price,
        'exit_pnl_pct': exit_pnl_pct,
        # 其他
        'hold_days': _hold_days(holding),
        'entry_pending': bool(holding.get('entry_pending')),
    }


def make_status_placeholder(holding: Dict, current_price: float = 0.0,
                            trigger: str = 'no_quote') -> Dict:
    """生成占位评估结果（无行情 / 待开盘价场景），保证报告字段齐全"""
    buy = _to_float(holding.get('buy_price'))
    activate = (_to_float(holding.get('trail_activate_pct'))
                or _to_float(holding.get('take_profit_pct')) or TRAIL_ACTIVATE_PCT)
    drawdown = _to_float(holding.get('trail_drawdown_pct')) or TRAIL_DRAWDOWN_PCT
    trail_high = _to_float(holding.get('trail_high')) or 0.0
    trail_active = bool(holding.get('trail_active'))
    return {
        'code': holding['code'], 'name': holding.get('name', ''),
        'buy_price': round(buy, 3),
        'buy_date': holding.get('buy_date'), 'entry_date': holding.get('entry_date'),
        'current_price': round(_to_float(current_price), 3),
        'day_high': 0.0, 'day_low': 0.0,
        'pnl_pct': 0.0, 'trigger': trigger,
        'status_text': STATUS_TEXT.get(trigger, ''),
        'stop_loss_pct': _to_float(holding.get('stop_loss_pct')) or DEFAULT_STOP_LOSS_PCT,
        'distance_to_stop_loss': 0.0,
        'trail_activate_pct': activate, 'trail_drawdown_pct': drawdown,
        'trail_active': trail_active,
        'trail_high': round(trail_high, 3) if trail_high else None,
        'trail_trigger_price': (round(trail_high * (1 - drawdown / 100), 3) if trail_high else None),
        'trail_peak_pnl': (round((trail_high / buy - 1) * 100, 2) if trail_high and buy else None),
        'trail_started': False,
        'distance_to_trail': None,
        'distance_to_take_profit': 0.0,
        'exit_price': None, 'exit_pnl_pct': None,
        'hold_days': _hold_days(holding),
        'entry_pending': bool(holding.get('entry_pending')),
    }


# ============= 写入 / 变更 =============
def add_to_portfolio(code: str, name: str, buy_price: float,
                     buy_date: Optional[str] = None,
                     stop_loss_pct: float = DEFAULT_STOP_LOSS_PCT,
                     trail_activate_pct: float = TRAIL_ACTIVATE_PCT,
                     trail_drawdown_pct: float = TRAIL_DRAWDOWN_PCT,
                     source: str = 'morning_screen',
                     take_profit_pct: Optional[float] = None) -> Optional[Dict]:
    """
    新增一只持仓。
    - 已存在（任意未平仓）→ 跳过
    - 仓位已满（>= MAX_HOLDINGS）→ 跳过
    - entry_pending=True：buy_price 为信号日收盘价，待次日开盘归一化
    - take_profit_pct：旧参数名，等价于 trail_activate_pct（向后兼容）
    返回新增的 holding（未新增则 None）
    """
    if take_profit_pct is not None:
        trail_activate_pct = take_profit_pct
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
        'trail_activate_pct': float(trail_activate_pct),
        'trail_drawdown_pct': float(trail_drawdown_pct),
        'trail_active': False,
        'trail_high': None,
        'take_profit_pct': float(trail_activate_pct),   # 兼容旧读者
        'alerted': False,
        'alerted_at': None,
        'closed': False,
        'closed_at': None,
        'closed_price': None,
        'closed_pnl': None,
        'close_reason': None,
        'closed_peak_pnl': None,
        'source': source,
    }
    holdings.append(holding)
    save_portfolio(portfolio)
    print(f'  ➕ 加入持仓池: {name}({code}) 参考价 {buy_price:.2f} '
          f'止损{stop_loss_pct}% / 移动止盈启动+{trail_activate_pct}%（回撤{trail_drawdown_pct}%）'
          f'（来源：{source}）')
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
    把「待定入场价」的持仓按建仓日开盘价归一化（贴近模拟盘实际买入成本）。

    ⚠️ 日期判断（关键）：
        模拟盘口径是「信号次日开盘价成交」，所以只有在 **行情日严格晚于信号日**
        （data_date > buy_date）时才允许归一化。
        - 信号当日（同日重复跑复盘 / 早盘选股后当天复盘）→ 跳过，保持待定；
        - 行情日早于信号日（数据回退）→ 跳过，避免用错误日期的开盘价污染成本。
        这样保证成本基准永远是「信号次日的真实开盘价」，且重复运行幂等。

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

        # ---- 日期判断：行情日必须严格晚于信号日 ----
        sig_date = _fmt_date(h.get('buy_date')) if h.get('buy_date') else ''
        if not sig_date:
            print(f'  ⚠️ {h.get("name")}({h["code"]}) 缺少信号日 buy_date，跳过归一化')
            continue
        if date_str <= sig_date:
            print(f'  ⏳ {h.get("name")}({h["code"]}) 等待建仓日开盘价'
                  f'（行情日 {date_str} ≤ 信号日 {sig_date}，暂不归一化）')
            continue

        info = price_map.get(h['code'])
        if not info:
            continue
        open_px = _to_float(info.get('open'))
        if open_px <= 0:
            continue
        old = h.get('buy_price')
        h['buy_price'] = round(open_px, 3)
        h['entry_pending'] = False
        h['entry_date'] = date_str
        # 建仓当日重置移动止盈状态（以真实成本为基准重新起算）
        h['trail_active'] = False
        h['trail_high'] = None
        changed.append({'code': h['code'], 'name': h.get('name', ''),
                        'old': old, 'new': h['buy_price'], 'entry_date': date_str,
                        'signal_date': sig_date})

    if changed:
        save_portfolio(portfolio)
        for c in changed:
            print(f'  🔧 归一化入场价: {c["name"]}({c["code"]}) '
                  f'{c["old"]} → {c["new"]}（信号日 {c["signal_date"]} → '
                  f'建仓日 {c["entry_date"]} 开盘）')
    return changed


def update_trail_states(states: List[Dict]) -> int:
    """
    批量落盘移动止盈状态（一次写盘）。
    states: [{'code': '601975', 'trail_active': True, 'trail_high': 5.12}, ...]
    返回实际更新的条数。
    """
    if not states:
        return 0
    portfolio = load_portfolio()
    by_code = {str(s.get('code')).zfill(6): s for s in states if s.get('code')}
    updated = 0
    for h in portfolio.get('holdings', []):
        if h.get('closed'):
            continue
        s = by_code.get(h['code'])
        if not s:
            continue
        new_active = bool(s.get('trail_active'))
        new_high = s.get('trail_high')
        new_high = round(_to_float(new_high), 3) if new_high else None
        if h.get('trail_active') != new_active or h.get('trail_high') != new_high:
            h['trail_active'] = new_active
            h['trail_high'] = new_high
            updated += 1
    if updated:
        save_portfolio(portfolio)
    return updated


def close_holding(code: str, pnl_pct: float, reason: str = 'stop_loss',
                  price: Optional[float] = None,
                  peak_pnl: Optional[float] = None) -> Optional[Dict]:
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
            if peak_pnl is not None:
                h['closed_peak_pnl'] = round(float(peak_pnl), 2)
            target = h
            break
    if target:
        _prune_closed(portfolio)
        save_portfolio(portfolio)
    return target


def mark_alerted(code: str) -> None:
    """标记某只持仓已推送过「接近启动线」提醒（避免重复提醒）"""
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
            trail = (f"移动止盈中 峰值{h.get('trail_high')}" if h.get('trail_active')
                     else f"移动止盈待启动(+{h.get('trail_activate_pct')}%)")
            print(f"  {flag} {h['name']}({h['code']}) @ {h['buy_price']} "
                  f"止损{h['stop_loss_pct']}% / {trail} 来源={h.get('source')}")
        recent = get_recent_closed_holdings(5)
        if recent:
            print(f'\n📜 最近平仓（{len(recent)} 条）：')
            for h in recent:
                peak = h.get('closed_peak_pnl')
                peak_txt = f" 峰值{peak:+.2f}%" if peak is not None else ''
                print(f"  [{h.get('close_reason')}] {h['name']}({h['code']}) "
                      f"{h.get('closed_pnl'):+.2f}%{peak_txt} @ {h.get('closed_at')}")
        stats = portfolio_stats()
        if stats['total']:
            print(f"\n📊 累计战绩：{stats['total']} 笔，胜率 {stats['win_rate']}%，"
                  f"平均 {stats['avg_pnl']:+.2f}%")
    elif arg == 'clear':
        clear_portfolio()
        print('🧹 持仓池已清空')
    else:
        print('用法: python src/portfolio.py [show|clear]')
