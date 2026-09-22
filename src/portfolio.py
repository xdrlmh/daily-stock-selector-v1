"""
持仓池管理模块（模拟盘）
==========================
设计要点：
1. 最多同时持有 MAX_HOLDINGS(3) 只；
2. 买入价按「信号日次日开盘价」归一化（贴近模拟盘真实买入成本）；
3. 离场＝**大波段口径（2026-09-22 起）**：固定止损 −7% ＋ 均线离场（破 MA10 减半 /
   破 MA20 清仓），由「大盘弱势熔断」在暴跌日暂缓其中的减仓动作；
4. 平仓后自动补仓（由盘后复盘流程调用，详见 main_review.py）；
5. 持仓池持久化到 data/portfolio.json（GitHub Actions 中由 workflow 回写仓库）。

★★ 离场决策树（唯一口径，按优先级；判定价 = **当日收盘价**）
--------------------------------------------------------------------------
  ① 固定止损   收盘盈亏 ≤ EXIT_STOP_LOSS_PCT(-7%)           → 全部清仓
  ② 破 MA20    收盘价 < MA20                                → 全部清仓（含已减半的剩余仓位）
  ③ 移动止盈   盈利≥+15% 启动 → 峰值回撤 8%                → 全部清仓（**默认停用**）
  ④ 破 MA10    收盘价 < MA10 且**尚未减半**                 → 卖出 50%（减半仓）
  ⑤ 时间止损   僵尸/低效股                                  → 全部清仓（**默认停用**）

★★ 「整体协调」的三条铁律（2026-09-22 用户拍板 + 实测支撑）
--------------------------------------------------------------------------
  1. **事件型信号不可暂缓**：固定止损一旦触及必须执行 —— 次日若反弹，该信号会**永久消失**，
     暂缓 = 直接漏掉风控。⇒ 熔断**永不拦**固定止损。
  2. **趋势硬底线不可暂缓**：破 MA20 是趋势终结判定，且它承担了「−7% 之上」的主要全平职责。
     实测：一旦拦它，收益 −0.13pt、最深浮亏恶化 0.23pt、止损打满仓次数 79→84。
     ⇒ 熔断**不拦**破 MA20 清仓。
  3. **节奏型信号可暂缓一天**：破 MA10 减半属于「减仓换手」而非「风险出清」，且是**状态型**
     信号（次日仍破位会自然重判，不存在漏执行）。暴跌日个股普遍假破 MA10，
     暂缓一天的成本实测 ≈ 0（+0.01pt；15 次暂缓中有 4 次次日站回 MA10 = 避免假破）。
     ⇒ 熔断**只拦**破 MA10 减半。
  ⇒ 三个闸门因此各司其职、互不越权：止损管「价格硬底线」、MA20 管「趋势终结」、
     MA10 管「节奏减仓」，熔断管「暴跌日不追跌减仓」。**没有任何动作会被熔断吞掉**。

大盘弱势熔断（2026-09-11 新增，2026-09-22 改口径）
- 触发：上证指数当日跌幅 ≤ MARKET_FUSE_DROP_PCT(-2.0%)
- 作用：① 当天**暂缓「破 MA10 减半」**（暴跌日不追跌减仓，次日企稳自动补执行）；
        ② 当天**暂停补仓 / 新开仓**（暴跌日既不砍也不加，口径自洽）
- 不拦：固定止损 −7%、破 MA20 清仓、移动止盈（均为保护性纪律）
- 数据缺失（指数涨跌幅为 None/NaN）→ **不熔断**，避免因取数失败导致长期停摆

半仓（2026-09-22 新增）
- 触发：收盘价 < MA10 且尚未减半 → 卖出 50%，落盘 `half_sold=True` 等字段
- **不补仓**：破 MA10 后不回补那 50%（用户拍板）；该票**仍占 1 个持仓名额**直至清仓
- 减半仓**不改变**买入成本 `buy_price` ⇒ 固定止损 −7% 仍以原始成本为基准（不是移动止损）
- 统计口径：一笔「减半 + 后续清仓」的交易**合并为 1 笔**，
  `closed_pnl` = 0.5×半仓段收益 + 0.5×剩余段收益（另存 `half_sold_pnl` / `closed_pnl_leg2` 留痕）
- 幂等：`half_sold` 为真时不再重复减半（同日重复跑复盘安全）

触发优先级：**固定止损 > 破MA20清仓 > 移动止盈 > 破MA10减半 > 时间止损 > 接近启动线提醒**
（②③ 均为全平类、④ 为减仓类、⑤ 为换仓类；顺序固定以保证可复现。②与①同日触发时成交价同为收盘价，
故顺序不影响金额，仅决定 `close_reason` 标注。）

数据模型（version 2）
{
  "version": 2,
  "updated_at": "2026-09-22 18:30:00",
  "holdings": [
    {
      "code": "601975", "name": "招商南油",
      "buy_price": 4.48,               # entry_pending=true 时为信号日收盘价
      "buy_date": "2026-09-22",        # 信号日
      "entry_date": null,              # 实际建仓日（=信号次日，归一化后写入）
      "entry_pending": true,           # true=等待次日开盘价归一化
      "shares": 0,
      "stop_loss_pct": -7.0,           # 固定止损
      "half_ratio": 0.5,               # 🆕 破 MA10 减半的比例
      "half_sold": false,              # 🆕 是否已减半（幂等开关）
      "half_sold_at": null,            # 🆕 减半落盘时间
      "half_sold_date": null,          # 🆕 减半的行情日
      "half_sold_price": null,         # 🆕 减半成交价
      "half_sold_pnl": null,           # 🆕 减半段收益%
      "remaining_ratio": 1.0,          # 🆕 剩余仓位比例（减半后 0.5）
      "trail_activate_pct": 15.0,      # 移动止盈启动线（默认停用）
      "trail_drawdown_pct": 8.0,
      "trail_active": false,
      "trail_high": null,
      "days_held": 0,
      "last_review_date": null,
      "peak_high": null,
      "alerted": false, "alerted_at": null,
      "closed": false, "closed_at": null,
      "closed_price": null, "closed_pnl": null, "close_reason": null,
      "closed_pnl_leg2": null,         # 🆕 最后一段（未减半时=全仓）收益%
      "closed_peak_pnl": null,
      "closed_days_held": null,
      "source": "morning_screen"       # morning_screen | review_refill
    }
  ]
}
"""
import json
import os
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Optional, Any

# ============= 环境变量覆盖（应急调整 / 云端验证用）=============
def _env_float(name: str, default: float) -> float:
    """用环境变量覆盖数值参数（空值 / 非法值 → 回退默认值）"""
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == '':
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    """用环境变量覆盖布尔开关（1/true/yes/on → True）"""
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == '':
        return default
    return str(raw).strip().lower() in ('1', 'true', 'yes', 'on')


# ============= 路径配置 =============
PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / 'data'
PORTFOLIO_PATH = DATA_DIR / 'portfolio.json'

# ============= 持仓参数 =============
MAX_HOLDINGS = 3                 # 最大同时持仓数（模拟盘买进头 3 只）
MAX_CLOSED_KEEP = 30             # 仅保留最近 N 条已平仓记录，防止文件无限膨胀

# ============= 离场规则（大波段口径，2026-09-22 起）=============
# ★ 参数单一来源：调参只改这一处。
# 决策树：① 固定止损 → ② 破MA20清仓 → ③ 移动止盈(默认停用) → ④ 破MA10减半 → ⑤ 时间止损(默认停用)
EXIT_USE_STOP = _env_bool('EXIT_USE_STOP', True)          # ① 固定止损总开关
EXIT_STOP_LOSS_PCT = _env_float('EXIT_STOP_LOSS_PCT', -7.0)   # 收盘盈亏 ≤ 此值 → 全部清仓
EXIT_USE_MA = _env_bool('EXIT_USE_MA', True)              # ②④ 均线离场总开关
EXIT_MA_HALF = 10                # ④ 破 MA10 → 减半仓（需 ≥10 根有效收盘价，否则本闸门不生效）
EXIT_MA_CLEAR = 20               # ② 破 MA20 → 全部清仓（含已减半的剩余仓位）
EXIT_MA_HALF_RATIO = _env_float('EXIT_MA_HALF_RATIO', 0.5)    # 减半比例（0.5 = 卖一半）
DEFAULT_STOP_LOSS_PCT = EXIT_STOP_LOSS_PCT   # 兼容旧字段名 / 旧调用

# 旧离场子系统（2026-09-22 起默认停用，代码保留 → 环境变量可零成本回滚）
TRAIL_ENABLED = _env_bool('TRAIL_ENABLED', False)         # ③ 移动止盈（默认关）
TRAIL_ACTIVATE_PCT = 15.0        # 盈利达到 +15% 启动移动止盈
TRAIL_DRAWDOWN_PCT = 8.0         # 启动后从最高价回撤 8% → 卖出（3=激进锁利 / 8=宽吃趋势）
WARNING_PROFIT_PCT = 10.0        # 接近启动线的提醒阈值
DEFAULT_TAKE_PROFIT_PCT = TRAIL_ACTIVATE_PCT   # 兼容旧字段/旧调用（含义＝启动线）

# ============= 时间止损参数（清理僵尸股 / 低效股）· ⑤ 默认停用 =============
# 逻辑：仓位只有 3 个，长期不走出趋势的票要主动让位，否则错过新的主升浪机会。
# 判定口径统一用「期间最高涨幅 peak_high_pnl」（持有期内最高价，只上不下），
# 比看当前盈亏公平：单日大盘普跌不会把本来形态不错的票误杀。
TIME_STOP_ENABLED = _env_bool('TIME_STOP_ENABLED', False)   # ⚠️ 2026-09-22 起默认 False
ZOMBIE_DAYS = 8                  # 🧟 僵尸股考察期（交易日）——约 2 周（稳健档）
ZOMBIE_PEAK_PCT = 3.0            #    期间最高涨幅低于此值 → 判为僵尸（从没像样动过）
INEFFICIENT_DAYS = 15            # 🐌 低效股考察期（交易日）——约 3 周（稳健档）
INEFFICIENT_PEAK_PCT = 5.0       #    期满仍未启动移动止盈且峰值低于此值 → 判为低效
# 参考档位：激进 3/7 ｜ 快档 5/10 ｜ 原·稳健 8/15

# ============= 大盘弱势熔断参数（2026-09-11 新增 / 2026-09-22 改口径）=============
# 逻辑：大盘暴跌日不做「追跌减仓」与「主动加仓」——暴跌往往泥沙俱下，
#      此时减半仓容易卖在最低点，次日大盘企稳又得重新追高。
# ★ 作用范围（MARKET_FUSE_SCOPE）＝ 只拦「破 MA10 减半」＋ 暂停补仓；
#   固定止损 −7% 与破 MA20 清仓**照常执行**（保护性纪律，不因大盘弱而放宽）。
# 取数：复用 fetch_market_review() 的上证指数涨跌幅（Tushare index_daily）
MARKET_FUSE_ENABLED = _env_bool('MARKET_FUSE_ENABLED', True)
MARKET_FUSE_INDEX = '上证指数'    # 判定基准（仅供文案展示）
MARKET_FUSE_DROP_PCT = _env_float('MARKET_FUSE_DROP_PCT', -2.0)
MARKET_FUSE_SCOPE = 'ma10_half'  # 熔断作用范围：仅「破 MA10 减半」这一减仓动作
MARKET_FUSE_BLOCK_REFILL = _env_bool('MARKET_FUSE_BLOCK_REFILL', True)  # 熔断日暂停补仓/新开仓
# 以上参数支持环境变量覆盖，便于应急调整；云端验证时可用极端阈值（如 99）强制触发熔断来验证拦截路径。

STATUS_TEXT = {
    'stop_loss': '🚨 触发止损',
    'ma20_exit': '🔻 破MA20 清仓',
    'ma10_half': '✂️ 破MA10 减半仓',
    'ma10_half_fused': '✂️ 破MA10 减半·熔断暂缓',
    'half_holding': '◐ 半仓持有（已破MA10）',
    'take_profit': '🔒 移动止盈卖出',
    'trailing': '🔒 移动止盈中',
    'warning': '⚡ 接近启动线',
    'normal': '🟢 正常持有',
    'pending': '⏳ 待开盘价',
    'no_quote': '❓ 无行情',
    'time_stop_zombie': '🧟 僵尸股清理',
    'time_stop_inefficient': '🐌 低效股清理',
    'time_stop_zombie_fused': '🧟 僵尸股清理·熔断暂缓',
    'time_stop_inefficient_fused': '🐌 低效股清理·熔断暂缓',
}

# 时间止损平仓原因（供上层判断/渲染）
TIME_STOP_REASONS = ('time_stop_zombie', 'time_stop_inefficient')

# ★ 熔断可拦下的触发原因（协调铁律：只含「节奏型减仓」的信号，绝不含止损/MA20清仓）
FUSE_BLOCKABLE_TRIGGERS = TIME_STOP_REASONS + ('ma10_half',)

# 熔断暂缓对应的状态键（reason → fused 状态文案）
FUSE_STATUS_KEY = {
    'time_stop_zombie': 'time_stop_zombie_fused',
    'time_stop_inefficient': 'time_stop_inefficient_fused',
    'ma10_half': 'ma10_half_fused',
}

# 全平类触发原因（用于上层判断「本次是否已清仓」）
FULL_EXIT_REASONS = ('stop_loss', 'ma20_exit', 'take_profit') + TIME_STOP_REASONS
# 减仓类触发原因（不清仓，只减半）
HALF_EXIT_REASONS = ('ma10_half',)


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
    """老版本持仓补齐移动止盈 / 时间止损 / 半仓 字段（幂等）"""
    for h in portfolio.get('holdings', []):
        h.setdefault('stop_loss_pct', DEFAULT_STOP_LOSS_PCT)
        if 'trail_activate_pct' not in h:
            h['trail_activate_pct'] = _to_float(h.get('take_profit_pct')) or TRAIL_ACTIVATE_PCT
        h.setdefault('trail_drawdown_pct', TRAIL_DRAWDOWN_PCT)
        h.setdefault('trail_active', False)
        h.setdefault('trail_high', None)
        h.setdefault('closed_peak_pnl', None)
        # ---- 时间止损字段 ----
        # ⚠️ 老持仓（无 days_held）采取「保守重启」：交易日钟从 0 起算，
        #    避免因缺少历史峰值而把真实存在的浮盈误判成僵尸股（宁可晚清，不可错清）。
        h.setdefault('days_held', 0)
        h.setdefault('last_review_date', None)
        h.setdefault('peak_high', None)
        h.setdefault('closed_days_held', None)
        # ---- 半仓（破 MA10 减半）字段 ----
        # ⚠️ 老持仓一律视为「未减半」：不追溯补扣半仓，避免用历史数据反推当时的 MA10 破位。
        h.setdefault('half_ratio', EXIT_MA_HALF_RATIO)
        h.setdefault('half_sold', False)
        h.setdefault('half_sold_at', None)
        h.setdefault('half_sold_date', None)
        h.setdefault('half_sold_price', None)
        h.setdefault('half_sold_pnl', None)
        # remaining_ratio 由 half_sold 派生，保持自洽（不信任文件里的旧值）
        h['remaining_ratio'] = (1.0 - float(h['half_ratio'])) if h.get('half_sold') else 1.0
        h.setdefault('closed_pnl_leg2', None)
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
        return {'total': 0, 'wins': 0, 'losses': 0, 'win_rate': None,
                'avg_pnl': None, 'take_profits': 0, 'stop_losses': 0,
                'time_stops': 0, 'avg_days_held': None, 'half_exits': 0,
                'ma20_exits': 0}
    wins = [h for h in closed if h['closed_pnl'] > 0]
    losses = [h for h in closed if h['closed_pnl'] <= 0]
    held = [h['closed_days_held'] for h in closed
            if h.get('closed_days_held') is not None]
    return {
        'total': len(closed),
        'wins': len(wins),
        'losses': len(losses),
        'win_rate': round(len(wins) / len(closed) * 100, 1),
        # ⚠️ avg_pnl 用 closed_pnl（已减半的持仓＝加权总收益），口径与「1 笔」一致
        'avg_pnl': round(sum(h['closed_pnl'] for h in closed) / len(closed), 2),
        'take_profits': len([h for h in closed if h.get('close_reason') == 'take_profit']),
        'stop_losses': len([h for h in closed if h.get('close_reason') == 'stop_loss']),
        'time_stops': len([h for h in closed
                           if h.get('close_reason') in TIME_STOP_REASONS]),
        # 🆕 破 MA20 清仓笔数 / 曾减半仓的笔数（含「减半后又破MA20」）
        'ma20_exits': len([h for h in closed if h.get('close_reason') == 'ma20_exit']),
        'half_exits': len([h for h in closed if h.get('half_sold')]),
        'avg_days_held': round(sum(held) / len(held), 1) if held else None,
    }


# ============= 评估 =============
def _hold_days(holding: Dict) -> Optional[int]:
    """
    已持有**交易日**数（由 advance_position_clock 逐日累加）。
    老数据无该字段时退化为自然日估算，仅为展示用，不参与时间止损判定之外的计算。
    """
    v = holding.get('days_held')
    if v is not None:
        try:
            return int(v)
        except (TypeError, ValueError):
            pass
    base = holding.get('entry_date') or holding.get('buy_date')
    if not base:
        return None
    try:
        d0 = datetime.strptime(_fmt_date(base), '%Y-%m-%d')
    except (ValueError, TypeError):
        return None
    return max(0, int((datetime.now() - d0).days * 5 / 7))


def market_fuse_check(index_pct_change: Optional[float],
                      enabled: Optional[bool] = None,
                      drop_pct: Optional[float] = None):
    """
    大盘弱势熔断判定。

    ★ 作用范围（2026-09-22 起）＝ **只拦「破 MA10 减半」这一减仓动作** + 暂停补仓/新开仓：
      - 均线离场中的「破 MA10 减半」是**状态型**信号 → 次日企稳会自动重判，不存在漏执行；
        暴跌日个股普遍假破 MA10，暂缓一天的成本实测 ≈ 0（15 次暂缓中 4 次次日站回 = 避免假破）。
      - **固定止损 −7%**（事件型：次日反弹则信号永久消失）与**破 MA20 清仓**（趋势硬底线）
        **照常执行**，绝不拦 —— 实测拦它们会使收益 −0.13pt、最深浮亏恶化 0.23pt。

    - index_pct_change：基准指数当日涨跌幅（%），来自 fetch_market_review()['index_pct_change']
    - None / NaN / 非法值 → **不熔断**（取数失败时不阻塞正常流程）
    - 返回 (是否熔断, 说明文案)

    例：上证指数 -2.35% 且阈值 -2.0% → (True, '上证指数 -2.35%（≤-2.0%）→ 今日暂缓破MA10减半 + 暂停补仓…')
    """
    if enabled is None:
        enabled = MARKET_FUSE_ENABLED
    if drop_pct is None:
        drop_pct = MARKET_FUSE_DROP_PCT
    if not enabled:
        return False, ''
    if index_pct_change is None:
        return False, ''
    try:
        pct = float(index_pct_change)
    except (TypeError, ValueError):
        return False, ''
    if pct != pct:          # NaN 自比不相等
        return False, ''
    if pct <= drop_pct:
        extra = '，并暂停补仓/新开仓' if MARKET_FUSE_BLOCK_REFILL else ''
        return True, (f'{MARKET_FUSE_INDEX} {pct:+.2f}%（≤{drop_pct:.1f}%）'
                      f' → 今日暂缓「破MA10减半」{extra}；'
                      f'固定止损 −7% 与破MA20清仓照常执行，大盘企稳后自动补执行')
    return False, ''


def mark_fused(evaluation: Dict, fuse_msg: str = '') -> Dict:
    """
    把「已被熔断范围内的信号命中、但因大盘熔断暂缓执行」的评估结果就地打标（返回同一对象）。

    - 可拦范围 = FUSE_BLOCKABLE_TRIGGERS（破MA10减半 / 时间止损）
    - 加 `fused=True` / `fuse_reason`
    - 状态文案改为 `✂️ 破MA10 减半·熔断暂缓`，避免报告里看起来像已经减仓
    """
    reason = evaluation.get('trigger')
    if reason in FUSE_BLOCKABLE_TRIGGERS:
        evaluation['fused'] = True
        evaluation['fuse_reason'] = fuse_msg
        key = FUSE_STATUS_KEY.get(reason)
        if key:
            evaluation['status_text'] = STATUS_TEXT[key]
    return evaluation


def fuse_blocks(action: str, fused: bool) -> bool:
    """★ 单一协调入口：判断某类离场动作当天是否被熔断拦下。

    action 取 'stop_loss' / 'ma20_exit' / 'take_profit' / 'ma10_half' / 'time_stop_*'。
    只有 FUSE_BLOCKABLE_TRIGGERS 内的动作（破MA10减半 / 时间止损）才可能被拦
    → 固定止损与破MA20清仓**永远返回 False**。
    （供上层与测试直接断言「熔断不会吞掉风控动作」，不必读实现细节。）
    """
    if not fused:
        return False
    return action in FUSE_BLOCKABLE_TRIGGERS


def _time_stop_check(days_held: int, peak_high_pnl: Optional[float]) -> Optional[str]:
    """
    时间止损判定（仅在**未启动移动止盈**时调用）。

    - 僵尸股：持有 ≥ ZOMBIE_DAYS 且 期间最高涨幅 < ZOMBIE_PEAK_PCT
    - 低效股：持有 ≥ INEFFICIENT_DAYS 且 期间最高涨幅 < INEFFICIENT_PEAK_PCT

    peak_high_pnl 为 None（尚无峰值观测）时不判定，避免误杀。
    返回 'time_stop_zombie' / 'time_stop_inefficient' / None
    """
    if not TIME_STOP_ENABLED or peak_high_pnl is None:
        return None
    if days_held >= ZOMBIE_DAYS and peak_high_pnl < ZOMBIE_PEAK_PCT:
        return 'time_stop_zombie'
    if days_held >= INEFFICIENT_DAYS and peak_high_pnl < INEFFICIENT_PEAK_PCT:
        return 'time_stop_inefficient'
    return None


def _time_stop_hint(days_held: int, peak_high_pnl: Optional[float],
                    trail_active: bool) -> Optional[str]:
    """时间止损「观察中」提示（未触发时给持有者一个倒计时，便于提前决策）"""
    if not TIME_STOP_ENABLED or trail_active or peak_high_pnl is None:
        return None
    if peak_high_pnl < ZOMBIE_PEAK_PCT:
        if days_held >= max(1, ZOMBIE_DAYS // 2):
            return f'🧟 僵尸观察(剩 {max(0, ZOMBIE_DAYS - days_held)} 日)'
    elif peak_high_pnl < INEFFICIENT_PEAK_PCT:
        if days_held >= max(1, INEFFICIENT_DAYS // 2):
            return f'🐌 低效观察(剩 {max(0, INEFFICIENT_DAYS - days_held)} 日)'
    return None


def evaluate_holding(holding: Dict, current_price: float,
                     day_high: Optional[float] = None,
                     day_low: Optional[float] = None,
                     ma10: Optional[float] = None,
                     ma20: Optional[float] = None,
                     fused: bool = False) -> Optional[Dict]:
    """
    评估单只持仓（纯计算，不落盘）。

    ★ 离场决策树（判定价 = **当日收盘价**；顺序固定，保证可复现）：
      ① 固定止损  收盘盈亏 ≤ stop（默认 -7%）              → 全部清仓｜熔断**不拦**
      ② 破 MA20   收盘价 < MA20                            → 全部清仓（含已减半的剩余仓位）｜熔断**不拦**
      ③ 移动止盈  浮盈首破 +15% 启动 → 峰值回撤 8%         → 全部清仓｜TRAIL_ENABLED=False 时整段跳过
      ④ 破 MA10   收盘价 < MA10 且**尚未减半**             → 卖出 50%｜**熔断可暂缓一天**
      ⑤ 时间止损  僵尸 / 低效股                            → 全部清仓｜TIME_STOP_ENABLED=False 时整段跳过
      ⑥ 半仓持有  已减半且未触及其它闸门                    → 只标记，不动作
      ⑦ 警戒      浮盈 ≥ +10% 且尚未启动移动止盈 → 一次性提醒

    ma10 / ma20：收盘均线（None / NaN / ≤0 → **该闸门本次不生效**，退化为只走其余闸门）
    fused：当日大盘熔断是否生效 → 仅影响 ④（与时停，若启用）；①②③ **一律照常执行**
    day_high / day_low：当日盘中最高/最低价（缺省时退化为用收盘价判断）

    返回 dict；无有效行情时返回 None。
    额外返回（供调用方落盘/渲染）：
      trail_active / trail_high / trail_started（本次是否新启动）
      exit_price / exit_pnl_pct（触发**全部清仓**时的成交价与盈亏）
      half_price / half_pnl_pct（触发**减半仓**时的成交价与盈亏）
      is_full_exit / is_half_exit / fused（本次动作类型）
      weighted_pnl_pct（若此刻清仓的**加权总收益**：已减半则 0.5×半仓段 + 0.5×当前段）
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

    def _valid(v: Any) -> Optional[float]:
        """均线有效性：None/NaN/≤0 一律视为「该闸门本次不生效」"""
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        if f != f or f <= 0:
            return None
        return f

    ma10 = _valid(ma10)
    ma20 = _valid(ma20)

    # ---- 半仓状态 ----
    half_ratio = _to_float(holding.get('half_ratio')) or EXIT_MA_HALF_RATIO
    half_ratio = min(max(half_ratio, 0.0), 1.0)
    half_sold = bool(holding.get('half_sold'))
    half_sold_pnl = holding.get('half_sold_pnl')
    remaining_ratio = (1.0 - half_ratio) if half_sold else 1.0

    # ---- 时间止损相关字段 ----
    days_held = int(_to_float(holding.get('days_held')))
    peak_high = _to_float(holding.get('peak_high'))
    if high > peak_high:
        peak_high = high
    peak_high_pnl = _pnl_pct(peak_high, buy) if peak_high > 0 else None

    was_active = bool(holding.get('trail_active'))
    trail_active = was_active
    trail_high = _to_float(holding.get('trail_high')) or 0.0
    trail_started = False
    exit_price: Optional[float] = None
    exit_pnl_pct: Optional[float] = None
    half_price: Optional[float] = None
    half_pnl_pct: Optional[float] = None
    fused_hit = False
    taken = False

    # ---- ① 固定止损（事件型；熔断永不拦）----
    if EXIT_USE_STOP and pnl_pct <= stop:
        trigger = 'stop_loss'
        exit_price = round(current_price, 3)
        exit_pnl_pct = pnl_pct
        taken = True

    # ---- ② 破 MA20 清仓（趋势硬底线；含已减半的剩余仓位）----
    if not taken and EXIT_USE_MA and ma20 is not None and current_price < ma20:
        trigger = 'ma20_exit'
        exit_price = round(current_price, 3)
        exit_pnl_pct = pnl_pct
        taken = True

    # ---- ③ 移动止盈（默认停用，环境变量 TRAIL_ENABLED=1 可零成本回滚）----
    if not taken and TRAIL_ENABLED:
        if not trail_active:
            if _pnl_pct(max(high, current_price), buy) >= activate:
                trail_active = True
                trail_started = True
                trail_high = max(high, current_price)
        else:
            trail_high = max(trail_high, high, current_price)
        if trail_active:
            trigger_price = round(trail_high * (1 - drawdown / 100), 3)
            if low <= trigger_price:
                trigger = 'take_profit'
                exit_price = trigger_price
                exit_pnl_pct = _pnl_pct(trigger_price, buy)
                taken = True
            elif current_price <= trigger_price:
                trigger = 'take_profit'
                exit_price = round(current_price, 3)
                exit_pnl_pct = pnl_pct
                taken = True

    # ---- ④ 破 MA10 减半（节奏型；熔断可暂缓一天）----
    if not taken and EXIT_USE_MA and not half_sold and ma10 is not None and current_price < ma10:
        trigger = 'ma10_half'
        if fused:
            fused_hit = True              # 暂缓：本日不执行减半，次日自然重判
        else:
            half_price = round(current_price, 3)
            half_pnl_pct = pnl_pct
        taken = True

    # ---- ⑤ 时间止损（默认停用；仅未减半时考核）----
    if not taken and TIME_STOP_ENABLED and not half_sold:
        ts_reason = _time_stop_check(days_held, peak_high_pnl)
        if ts_reason:
            trigger = ts_reason
            if fused:
                fused_hit = True
            else:
                exit_price = round(current_price, 3)
                exit_pnl_pct = pnl_pct
            taken = True

    # ---- ⑥⑦ 无动作场景：半仓持有 / 移动止盈中 / 警戒 / 正常 ----
    if not taken:
        if half_sold:
            trigger = 'half_holding'
        elif TRAIL_ENABLED and trail_active:
            trigger = 'trailing'
        elif TRAIL_ENABLED and pnl_pct >= WARNING_PROFIT_PCT and not holding.get('alerted'):
            # ⚠️ 警戒只在移动止盈启用时有意义（否则「接近启动线」无从谈起）
            trigger = 'warning'
        else:
            trigger = 'normal'

    # ---- 汇总 ----
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

    # 加权总收益（一笔交易的真实口径：减半段 + 剩余段）
    if half_sold and half_sold_pnl is not None:
        weighted_pnl = round(_to_float(half_sold_pnl) * half_ratio + pnl_pct * remaining_ratio, 2)
    else:
        weighted_pnl = pnl_pct

    # 熔断暂缓 → 状态文案改用「…·熔断暂缓」，避免报告里看起来已经减仓/清仓
    status_text = STATUS_TEXT.get(
        FUSE_STATUS_KEY.get(trigger, trigger) if fused_hit else trigger, '')

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
        'status_text': status_text,
        # 动作类型（供上层单点判断）
        'is_full_exit': (trigger in FULL_EXIT_REASONS) and not fused_hit,
        'is_half_exit': (trigger in HALF_EXIT_REASONS) and not fused_hit,
        'fused': fused_hit,
        'fuse_reason': '',
        # 止损
        'stop_loss_pct': stop,
        'distance_to_stop_loss': round(pnl_pct - stop, 2),
        # 均线离场（🆕）
        'ma10': round(ma10, 3) if ma10 else None,
        'ma20': round(ma20, 3) if ma20 else None,
        'ma10_breached': bool(ma10 is not None and current_price < ma10),
        'ma20_breached': bool(ma20 is not None and current_price < ma20),
        'ma10_gap_pct': (round((current_price / ma10 - 1) * 100, 2) if ma10 else None),
        'ma20_gap_pct': (round((current_price / ma20 - 1) * 100, 2) if ma20 else None),
        # 半仓（🆕）
        'half_sold': half_sold,
        'half_ratio': round(half_ratio, 3),
        'remaining_ratio': round(remaining_ratio, 3),
        'half_sold_price': holding.get('half_sold_price'),
        'half_sold_pnl': half_sold_pnl,
        'half_price': half_price,
        'half_pnl_pct': half_pnl_pct,
        'weighted_pnl_pct': weighted_pnl,
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
        # 时间止损
        'days_held': days_held,
        'peak_high': round(peak_high, 3) if peak_high > 0 else None,
        'peak_high_pnl': peak_high_pnl,
        'time_stop_reason': trigger if trigger in TIME_STOP_REASONS else None,
        'time_stop_hint': (None if trigger in TIME_STOP_REASONS
                           else _time_stop_hint(days_held, peak_high_pnl, trail_active)),
        # 其他
        'hold_days': days_held,
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
        # 动作类型 / 熔断（占位场景不动作）
        'is_full_exit': False, 'is_half_exit': False,
        'fused': False, 'fuse_reason': '',
        # 均线离场（无行情 → 无均线可判）
        'ma10': None, 'ma20': None, 'ma10_breached': False, 'ma20_breached': False,
        'ma10_gap_pct': None, 'ma20_gap_pct': None,
        # 半仓
        'half_sold': bool(holding.get('half_sold')),
        'half_ratio': _to_float(holding.get('half_ratio')) or EXIT_MA_HALF_RATIO,
        'remaining_ratio': ((1.0 - (_to_float(holding.get('half_ratio')) or EXIT_MA_HALF_RATIO))
                            if holding.get('half_sold') else 1.0),
        'half_sold_price': holding.get('half_sold_price'),
        'half_sold_pnl': holding.get('half_sold_pnl'),
        'half_price': None, 'half_pnl_pct': None,
        'weighted_pnl_pct': 0.0,
        # 时间止损（占位场景不提示，避免误导）
        'days_held': _hold_days(holding),
        'peak_high': (_to_float(holding.get('peak_high')) or None),
        'peak_high_pnl': (_pnl_pct(_to_float(holding.get('peak_high')), buy)
                          if _to_float(holding.get('peak_high')) > 0 and buy > 0 else None),
        'time_stop_reason': None,
        'time_stop_hint': None,
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
        'half_ratio': EXIT_MA_HALF_RATIO,   # 🆕 破 MA10 减半比例
        'half_sold': False,                 # 🆕 是否已减半
        'half_sold_at': None,
        'half_sold_date': None,
        'half_sold_price': None,
        'half_sold_pnl': None,
        'remaining_ratio': 1.0,             # 🆕 剩余仓位比例
        'trail_activate_pct': float(trail_activate_pct),
        'trail_drawdown_pct': float(trail_drawdown_pct),
        'trail_active': False,
        'trail_high': None,
        'days_held': 0,                  # 待建仓期间不计持有天数
        'last_review_date': None,
        'peak_high': None,
        'take_profit_pct': float(trail_activate_pct),   # 兼容旧读者
        'alerted': False,
        'alerted_at': None,
        'closed': False,
        'closed_at': None,
        'closed_price': None,
        'closed_pnl': None,
        'close_reason': None,
        'closed_pnl_leg2': None,            # 🆕 最后一段（未减半时=全仓）收益%
        'closed_peak_pnl': None,
        'closed_days_held': None,
        'source': source,
    }
    holdings.append(holding)
    save_portfolio(portfolio)
    print(f'  ➕ 加入持仓池: {name}({code}) 参考价 {buy_price:.2f} '
          f'止损{stop_loss_pct}% / 破MA{EXIT_MA_HALF}减半 / 破MA{EXIT_MA_CLEAR}清仓'
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
        # 时间止损：建仓当日计为第 1 个交易日（当日开盘后即已持有）
        h['days_held'] = 1
        h['last_review_date'] = date_str
        h['peak_high'] = round(open_px, 3)
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


def advance_position_clock(price_map: Dict[str, Dict], data_date: str) -> List[Dict]:
    """
    推进活跃持仓的「交易日钟」与「期间最高价」（每次复盘调用一次，天然幂等）：

    1. **days_held**（已持有交易日数）：
       仅当 `data_date > last_review_date` 时 +1。
       → 同日重复跑复盘不会重复计数；周末/节假日因为行情日本身不前进，也不会多计；
         数据回退（行情日倒退）时同样不会计数。
    2. **peak_high**（持有期内最高价）：
       用当日盘中最高价更新（只上不下），作为时间止损「有没有表现」的判定依据。

    price_map: {code: {'open','price','high','low'}}
    data_date: 行情日期（YYYYMMDD 或 YYYY-MM-DD）
    返回被推进过的持仓列表（含 code/name/days_held/peak_high_pnl）。
    """
    date_str = _fmt_date(data_date)
    portfolio = load_portfolio()
    advanced: List[Dict] = []
    changed = False

    for h in portfolio.get('holdings', []):
        if h.get('closed') or h.get('entry_pending'):
            continue
        info = price_map.get(h['code']) or {}
        day_high = _to_float(info.get('high')) or _to_float(info.get('price'))

        # --- 期间最高价（只上不下）---
        prev_peak = _to_float(h.get('peak_high'))
        if day_high > prev_peak:
            h['peak_high'] = round(day_high, 3)
            changed = True

        # --- 交易日钟（仅新行情日 +1）---
        last = _fmt_date(h['last_review_date']) if h.get('last_review_date') else ''
        if date_str > last:
            h['days_held'] = int(_to_float(h.get('days_held'))) + 1
            h['last_review_date'] = date_str
            changed = True
            advanced.append({
                'code': h['code'], 'name': h.get('name', ''),
                'days_held': h['days_held'],
                'peak_high': h.get('peak_high'),
                'peak_high_pnl': _pnl_pct(_to_float(h.get('peak_high')),
                                          _to_float(h.get('buy_price'))),
            })

    if changed:
        save_portfolio(portfolio)
    for a in advanced:
        print(f"  ⏱️ {a['name']}({a['code']}) 已持有 {a['days_held']} 个交易日 ｜ "
              f"期间最高 {a['peak_high_pnl']:+.2f}%")
    return advanced


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


def apply_half_exit(code: str, price: float, pnl_pct: float,
                    date_str: Optional[str] = None,
                    ratio: Optional[float] = None) -> Optional[Dict]:
    """
    执行「破 MA10 减半仓」的**部分平仓**（幂等：已减半 → 直接返回 None，不重复扣减）。

    - 记录 half_sold / half_sold_at / half_sold_date / half_sold_price / half_sold_pnl
    - remaining_ratio = 1 − half_ratio
    - ⚠️ **不修改 buy_price** ⇒ 固定止损 −7% 仍以**原始成本**为基准（不是移动止损）
    - ⚠️ **不关仓**：该票仍是活跃持仓，继续占用 1 个名额；用户拍板「破 MA10 后不补仓」
    返回被减半的 holding（未减半 / 已减半过 / 未找到 → None）
    """
    code = str(code).zfill(6)
    ratio = EXIT_MA_HALF_RATIO if ratio is None else float(ratio)
    ratio = min(max(ratio, 0.0), 1.0)
    portfolio = load_portfolio()
    target = None
    for h in portfolio.get('holdings', []):
        if h['code'] == code and not h.get('closed'):
            if h.get('half_sold'):
                print(f'  ⏭️ {h.get("name")}({code}) 已减半仓，跳过（幂等）')
                return None
            h['half_sold'] = True
            h['half_ratio'] = ratio
            h['remaining_ratio'] = round(1.0 - ratio, 3)
            h['half_sold_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            h['half_sold_date'] = _fmt_date(date_str) if date_str else None
            h['half_sold_price'] = round(float(price), 3)
            h['half_sold_pnl'] = round(float(pnl_pct), 2)
            target = h
            break
    if target:
        save_portfolio(portfolio)
    return target


def close_holding(code: str, pnl_pct: float, reason: str = 'stop_loss',
                  price: Optional[float] = None,
                  peak_pnl: Optional[float] = None,
                  days_held: Optional[int] = None) -> Optional[Dict]:
    """标记某只持仓已清仓出场（全平）。返回被平仓的 holding（未找到则 None）

    ★ 统计口径（2026-09-22）：若该持仓曾「破 MA10 减半」，则 `closed_pnl` 记录
      **加权总收益** = half_ratio × 半仓段 + (1 − half_ratio) × 最后一段；
      另存 `closed_pnl_leg2`（最后一段）与 `half_sold_pnl`（半仓段）留痕。
      ⇒ 一笔「减半 + 后续清仓」合并为 **1 笔**，胜率 / 均值口径不失真。
    """
    code = str(code).zfill(6)
    portfolio = load_portfolio()
    target = None
    for h in portfolio.get('holdings', []):
        if h['code'] == code and not h.get('closed'):
            leg2 = round(float(pnl_pct), 2)
            h['closed'] = True
            h['closed_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            h['closed_price'] = round(float(price), 3) if price else None
            h['closed_pnl_leg2'] = leg2
            if h.get('half_sold') and h.get('half_sold_pnl') is not None:
                r = _to_float(h.get('half_ratio')) or EXIT_MA_HALF_RATIO
                r = min(max(r, 0.0), 1.0)
                h['closed_pnl'] = round(_to_float(h['half_sold_pnl']) * r + leg2 * (1.0 - r), 2)
            else:
                h['closed_pnl'] = leg2
            h['close_reason'] = reason
            if peak_pnl is not None:
                h['closed_peak_pnl'] = round(float(peak_pnl), 2)
            h['closed_days_held'] = (int(days_held) if days_held is not None
                                     else int(_to_float(h.get('days_held'))))
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
        print(f'   离场规则：止损{EXIT_STOP_LOSS_PCT}% ｜ 破MA{EXIT_MA_HALF}减半 ｜ 破MA{EXIT_MA_CLEAR}清仓'
              f' ｜ 熔断(≤{MARKET_FUSE_DROP_PCT}%)只拦MA{EXIT_MA_HALF}减半'
              f' ｜ 移动止盈{"启用" if TRAIL_ENABLED else "停用"}'
              f' ｜ 时间止损{"启用" if TIME_STOP_ENABLED else "停用"}')
        for h in active:
            if h.get('entry_pending'):
                flag = '⏳待归一化'
            else:
                flag = '◐已减半' if h.get('half_sold') else '🟢满仓'
            clock = ''
            if not h.get('entry_pending'):
                peak = _to_float(h.get('peak_high'))
                peak_txt = (f" 期间最高{peak:.2f}({_pnl_pct(peak, _to_float(h['buy_price'])):+.1f}%)"
                            if peak > 0 else '')
                clock = f" 持有{int(_to_float(h.get('days_held')))}日{peak_txt}"
                if h.get('half_sold'):
                    clock += (f" ｜ 半仓段{_to_float(h.get('half_sold_pnl')):+.2f}%"
                              f"@{h.get('half_sold_price')}({h.get('half_sold_date')})")
            print(f"  {flag} {h['name']}({h['code']}) @ {h['buy_price']} "
                  f"止损{h['stop_loss_pct']}% / 破MA{EXIT_MA_HALF}减半 / 破MA{EXIT_MA_CLEAR}清仓"
                  f"{clock} 来源={h.get('source')}")
        recent = get_recent_closed_holdings(5)
        if recent:
            print(f'\n📜 最近平仓（{len(recent)} 条）：')
            for h in recent:
                peak = h.get('closed_peak_pnl')
                peak_txt = f" 峰值{peak:+.2f}%" if peak is not None else ''
                half_txt = ''
                if h.get('half_sold'):
                    half_txt = (f"（加权：半仓段{h.get('half_sold_pnl'):+.2f}% ×{h.get('half_ratio')}"
                                f" + 尾段{h.get('closed_pnl_leg2'):+.2f}%）")
                print(f"  [{h.get('close_reason')}] {h['name']}({h['code']}) "
                      f"{h.get('closed_pnl'):+.2f}%{half_txt}{peak_txt} @ {h.get('closed_at')}")
        stats = portfolio_stats()
        if stats['total']:
            print(f"\n📊 累计战绩：{stats['total']} 笔，胜率 {stats['win_rate']}%，"
                  f"平均 {stats['avg_pnl']:+.2f}% ｜ 止损 {stats['stop_losses']} / "
                  f"破MA{EXIT_MA_CLEAR}清仓 {stats['ma20_exits']} / 含减半 {stats['half_exits']}")
            if stats['avg_days_held'] is not None:
                print(f"   平均持有 {stats['avg_days_held']} 个交易日")
    elif arg == 'clear':
        clear_portfolio()
        print('🧹 持仓池已清空')
    else:
        print('用法: python src/portfolio.py [show|clear]')
