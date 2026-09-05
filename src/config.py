"""配置模块 - 从环境变量读取钉钉 webhook 等敏感信息"""
import os
from pathlib import Path

# 钉钉配置（必填，通过 GitHub Secrets 注入）
DINGTALK_WEBHOOK = os.environ.get('DINGTALK_WEBHOOK', '').strip()
DINGTALK_SECRET = os.environ.get('DINGTALK_SECRET', '').strip()  # 可选，用于加签

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


def validate_config():
    """验证配置完整性"""
    if not DINGTALK_WEBHOOK and not TEST_ONLY:
        raise ValueError(
            '未配置 DINGTALK_WEBHOOK 环境变量。\n'
            '请在 GitHub 仓库 Settings → Secrets 中配置 DINGTALK_WEBHOOK。\n'
            '获取方式：\n'
            '1. 钉钉群 → 群设置 → 智能群助手 → 添加机器人 → 自定义\n'
            '2. 安全设置：自定义关键词 填「主升浪」\n'
            '3. 复制 webhook URL（形如 https://oapi.dingtalk.com/robot/send?access_token=xxx）'
        )


if __name__ == '__main__':
    validate_config()
    print(f'✅ 配置校验通过')
    print(f'  DINGTALK_WEBHOOK: {DINGTALK_WEBHOOK[:40]}...' if DINGTALK_WEBHOOK else '  DINGTALK_WEBHOOK: 未配置（TEST_ONLY 模式）')
    print(f'  DINGTALK_SECRET: {"已配置" if DINGTALK_SECRET else "未配置（普通模式）"}')
    print(f'  TEST_ONLY: {TEST_ONLY}')
    print(f'  REPORTS_DIR: {REPORTS_DIR}')