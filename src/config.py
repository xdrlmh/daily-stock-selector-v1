"""配置模块 - 从环境变量读取推送 webhook 等敏感信息"""
import os
from pathlib import Path

# ---------- 钉钉配置（通过 GitHub Secrets 注入）----------
DINGTALK_WEBHOOK = os.environ.get('DINGTALK_WEBHOOK', '').strip()
DINGTALK_SECRET = os.environ.get('DINGTALK_SECRET', '').strip()  # 可选，用于加签

# ---------- 飞书配置（通过 GitHub Secrets 注入）----------
FEISHU_WEBHOOK = os.environ.get('FEISHU_WEBHOOK', '').strip()
FEISHU_SECRET = os.environ.get('FEISHU_SECRET', '').strip()      # 可选，勾选「签名校验」时必填

# ---------- 推送通道开关 ----------
# 逗号分隔，默认双通道并行；未配置 webhook 的通道会自动跳过（不报错）
#   PUSH_CHANNELS=dingtalk          只推钉钉
#   PUSH_CHANNELS=feishu            只推飞书
#   PUSH_CHANNELS=dingtalk,feishu   双通道（默认）
PUSH_CHANNELS = os.environ.get('PUSH_CHANNELS', 'dingtalk,feishu').strip().lower()
CHANNELS = [c.strip() for c in PUSH_CHANNELS.split(',') if c.strip()]

# 测试模式（仅手动触发 workflow 时可设）
TEST_ONLY = os.environ.get('TEST_ONLY', 'false').lower() == 'true'

# 报告输出目录
PROJECT_ROOT = Path(__file__).parent.parent
REPORTS_DIR = PROJECT_ROOT / 'reports'
REPORTS_DIR.mkdir(exist_ok=True)

# 筛选参数
CONFIG = {
    'max_picks': 5,           # TOP 精选数量
    'max_warnings': 5,        # 警示名单数量
    'min_market_cap': 30e8,   # 最小市值 30 亿（过滤小盘股）
    'max_market_cap': 5000e8, # 最大市值 5000 亿（过滤超大盘）
    'high_rise_threshold_5d': 30,   # 5日涨幅 ≥ 30% 视为超买
    'high_rise_threshold_10d': 50,  # 10日涨幅 ≥ 50% 视为超买
    'turnover_overheat': 30,        # 换手 ≥ 30% 视为过热
    'main_inflow_ratio_threshold': 0.05,  # 主力净流出 > 5% 流通市值
}

# 评分权重（合计 100）
SCORE_WEIGHTS = {
    'technical': 25,
    'capital': 20,
    'valuation': 15,
    'catalyst': 15,
    'fundamental': 25,
}

# ============ 资金面因子（2026-09-22 大波段改造）============
# 背景：原「当日主力净流入」前 5% 在主板 3168 只 × 7 个信号日上 **7/7 全负超额**
#      （平均超额 −5.42%）—— 典型的「当日放量大涨 → 追高」。改「超大单持续流入」口径。
# 新口径（实证：主板 universe，过滤后超额 −0.87%，绝对额排序仍 −4.54%）：
#   A. 趋势确认：近 5 日**日均**超大单净额 > 近 60 日**日均** 且 > 0   → 10 分（否则 5 / 0）
#   B. 占比强度：近 5 日超大单净额 / 近 5 日成交额 的横截面占比        → 0~4 分
#   C. 换手率合理性（沿用原口径）                                     → 0~6 分
#   合计仍 = 20 分（SCORE_WEIGHTS['capital']）
#   ★ 硬过滤：`A` 不成立者**直接剔除候选池**（不占 TOP5 名额）—— 与实证口径一致。
CAPITAL_MODE = os.environ.get('CAPITAL_MODE', 'elg').strip().lower()
#   'elg'    = 新口径（超大单 5日/60日 趋势 + 占比），默认
#   'legacy' = 旧口径（当日主力净流入 0~14 分 + 换手 0~6 分），回退用

CAPITAL_TREND_FILTER = os.environ.get('CAPITAL_TREND_FILTER', 'on').strip().lower() \
    not in ('0', 'off', 'false', 'no')      # 关掉 = 只打分不剔除（回退用）

CAPITAL_SHORT_DAYS = 5        # 短周期窗口（日）
CAPITAL_LONG_DAYS = 60        # 长周期窗口（日）
CAPITAL_MIN_VALID_DAYS = 4    # 近 5 日有效数据门槛：<4 天视为「无信号」→ 资金面 0 分，
                              # 且**不计算占比**（防止停牌/次新导致占比虚高，实测 600825 仅 2 天数据算出 76%）
# 「数据不足」的归零范围（用户拍板原文「资金面给 0 分」→ 按字面＝整个维度归零）：
#   'all'      = 资金面整个 20 分归零（含换手率子项）—— 默认，拍板字面口径
#   'elg_only' = 只把「超大单趋势 / 占比」两项归零，换手率（与资金流数据无关）照常计分
# 切换成本为零（环境变量 CAPITAL_MISSING_ZERO_SCOPE），不影响其他任何逻辑。
CAPITAL_MISSING_ZERO_SCOPE = os.environ.get('CAPITAL_MISSING_ZERO_SCOPE', 'all').strip().lower()
CAPITAL_ELG_FULL = 10         # A 项：5日日均 > 60日日均 且 >0
CAPITAL_ELG_PARTIAL = 5       # A 项：5日日均 > 0 但未放大
CAPITAL_ELG_NONE = 0          # A 项：不满足 / 数据不足
# B 项档位：[(占比下限 %, 分)]，从高到低匹配（占比分布实测：中位 1.4% / p90 5.1% / p99 13.6%）
CAPITAL_RATIO_BANDS = ((5.0, 4), (2.0, 3), (0.5, 2), (0.0, 1))


def validate_config():
    """验证配置完整性：当前启用的通道中，至少要有一个配好了 webhook。

    注意：未配置 webhook 的通道会被「自动跳过」而不是报错，
    这样「只配了钉钉」的老部署升级后不会有任何影响。
    """
    if TEST_ONLY:
        return

    ok_dt = ('dingtalk' in CHANNELS) and bool(DINGTALK_WEBHOOK)
    ok_fs = ('feishu' in CHANNELS) and bool(FEISHU_WEBHOOK)
    if ok_dt or ok_fs:
        return

    if not DINGTALK_WEBHOOK and not FEISHU_WEBHOOK:
        raise ValueError(
            '未配置任何推送 webhook（DINGTALK_WEBHOOK / FEISHU_WEBHOOK）。\n'
            '请在 GitHub 仓库 Settings → Secrets 中至少配置一个。\n'
            '\n【钉钉】\n'
            '1. 钉钉群 → 群设置 → 智能群助手 → 添加机器人 → 自定义\n'
            '2. 安全设置：选「加签」或自定义关键词填「主升浪」\n'
            '3. 复制 webhook（形如 https://oapi.dingtalk.com/robot/send?access_token=xxx）\n'
            '\n【飞书】\n'
            '1. 飞书群 → 设置 → 群机器人 → 添加机器人 → 自定义机器人\n'
            '2. 安全设置：勾选「签名校验」（推荐）\n'
            '3. 复制 webhook（形如 https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx）\n'
            '4. 如勾选签名校验，另需配置 FEISHU_SECRET'
        )

    raise ValueError(
        f'没有可用的推送通道：PUSH_CHANNELS={PUSH_CHANNELS}，'
        f'但对应的 webhook 未配置。\n'
        f'当前 DINGTALK_WEBHOOK={"已配置" if DINGTALK_WEBHOOK else "未配置"} ｜ '
        f'FEISHU_WEBHOOK={"已配置" if FEISHU_WEBHOOK else "未配置"}'
    )


if __name__ == '__main__':
    validate_config()
    print(f'✅ 配置校验通过')
    print(f'  PUSH_CHANNELS: {CHANNELS}')
    print(f'  DINGTALK_WEBHOOK: {DINGTALK_WEBHOOK[:40]}...' if DINGTALK_WEBHOOK else '  DINGTALK_WEBHOOK: 未配置（跳过）')
    print(f'  DINGTALK_SECRET: {"已配置" if DINGTALK_SECRET else "未配置（普通模式）"}')
    print(f'  FEISHU_WEBHOOK: {FEISHU_WEBHOOK[:40]}...' if FEISHU_WEBHOOK else '  FEISHU_WEBHOOK: 未配置（跳过）')
    print(f'  FEISHU_SECRET: {"已配置" if FEISHU_SECRET else "未配置（普通模式）"}')
    print(f'  TEST_ONLY: {TEST_ONLY}')
    print(f'  REPORTS_DIR: {REPORTS_DIR}')