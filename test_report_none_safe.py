#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
report.py 空值健壮性测试
========================
线上曾因某只票 volume_ratio = None 导致 save_review_report 崩溃
（`unsupported format string passed to NoneType`），此处用极端缺失数据回归。

运行：python test_report_none_safe.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd

from src import report as rp

PASS, FAIL = 0, 0


def check(name, cond, detail=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f'  ✅ {name}')
    else:
        FAIL += 1
        print(f'  ❌ {name}  {detail}')


# 极端缺失：关键字段为 None / NaN
top = pd.DataFrame([
    {'code': '600104', 'name': '上汽集团', 'price': 11.72, 'pct_change': 3.10,
     'pct_60d': None, 'total_score': 88.0, 'main_net_inflow': 1.2e8,
     'turnover_rate': None, 'volume_ratio': None, 'pe_ttm': None,
     'circ_mcap': None, 'score_breakdown': {'题材': '汽车整车'}},
    {'code': '002011', 'name': '盾安环境', 'price': 13.29, 'pct_change': 2.20,
     'pct_60d': float('nan'), 'total_score': 85.0, 'main_net_inflow': None,
     'turnover_rate': float('nan'), 'volume_ratio': float('nan'), 'pe_ttm': float('nan'),
     'circ_mcap': float('nan'), 'score_breakdown': {}},
    {'code': '002443', 'name': '金洲管道', 'price': 11.81, 'pct_change': -1.10,
     'pct_60d': 5.0, 'total_score': None, 'main_net_inflow': 0,
     'turnover_rate': 3.2, 'volume_ratio': 1.1, 'pe_ttm': 18.0,
     'circ_mcap': 9.8e9, 'score_breakdown': {'题材': '管道'}},
])

print('=' * 62)
print('🧪 report.py 空值健壮性测试')
print('=' * 62)

print('\n【1】钉钉复盘 payload（generate_review_payload）')
try:
    payload = rp.generate_review_payload(
        date_str='2026-09-11', top_picks=top, warnings=pd.DataFrame(),
        all_stocks=pd.DataFrame(), market={'index_close': 3888.11, 'index_pct_change': -1.18},
        sector_heat=[], holdings_status=[], closed_today=[], new_positions=[],
        stats={'total': 0}, data_date='20260911',
    )
    text = payload['markdown']['text']
    check('payload 生成成功', True)
    check('精简表缺失值安全渲染（评分/主力显示 "-"）',
          '| 88 |' in text and '| - |' in text)
    check('明细表含全部 2 只票', '上汽集团' in text and '金洲管道' in text)
except Exception as e:
    check('payload 生成成功', False, f'{type(e).__name__}: {e}')

print('\n【2】完整复盘报告（save_review_report 落盘）')
try:
    with tempfile.TemporaryDirectory() as td:
        p = rp.save_review_report(
            date_str='2026-09-11', top_picks=top, warnings=pd.DataFrame(),
            all_stocks=pd.DataFrame(), market={'index_close': 3888.11},
            sector_heat=[], reports_dir=Path(td),
            holdings_status=[], closed_today=[], new_positions=[],
            stats={'total': 0}, data_date='20260911',
        )
        content = Path(p).read_text(encoding='utf-8')
        check('报告落盘成功', Path(p).exists())
        check('报告含全 3 只票', all(n in content for n in ['上汽集团', '盾安环境', '金洲管道']))
        check('报告缺失值渲染为 "-"', '- 量比：-' in content and '- 换手率：-' in content)
except Exception as e:
    check('报告落盘成功', False, f'{type(e).__name__}: {e}')

print('\n【3】早盘 payload（generate_dingtalk_payload）')
try:
    p2 = rp.generate_dingtalk_payload(
        date_str='2026-09-11', top_picks=top, warnings=pd.DataFrame(),
        all_stocks=pd.DataFrame(), market={'index_close': 3888.11},
    )
    check('早盘 payload 生成成功', bool(p2['markdown']['text']))
except Exception as e:
    check('早盘 payload 生成成功', False, f'{type(e).__name__}: {e}')

print('\n【4】明日展望 · 板块资金方向措辞（温度第一 ≠ 资金第一）')
# 复刻 2026-09-15 真实快照：半导体温度第 1 但主力净流出 18.3 亿
HEAT_OUT = [
    {'theme': '半导体', 'avg_pct': 0.5, 'inflow_yi': -18.3, 'limit_up': 1},
    {'theme': '家电', 'avg_pct': -0.6, 'inflow_yi': 1.8, 'limit_up': 0},
    {'theme': '人工智能', 'avg_pct': 0.0, 'inflow_yi': -6.9, 'limit_up': 1},
]
out_out = '\n'.join(rp.generate_outlook(
    {'trend_60d': 'sideways', 'main_net_inflow_yi': -242.1}, HEAT_OUT))
check('温度第一但资金净流出 → 不再谎称"资金聚焦"',
      '资金聚焦' not in out_out and '净流出 18.3亿' in out_out, out_out)
check('点明真实资金去向（家电 +1.8亿）',
      '资金实际流入方向是 **家电**' in out_out)

HEAT_IN = [{'theme': '家电', 'avg_pct': 2.1, 'inflow_yi': 9.5, 'limit_up': 2}]
out_in = '\n'.join(rp.generate_outlook(
    {'trend_60d': 'up', 'main_net_inflow_yi': 80}, HEAT_IN))
check('资金真流入时保留"资金聚焦"措辞',
      '资金聚焦 **家电**' in out_in and '主力 +9.5亿' in out_in)

# 极端健壮性：缺 theme / 值全为 None / NaN → 不得抛异常
try:
    out_none = '\n'.join(rp.generate_outlook({}, [
        {'avg_pct': None, 'inflow_yi': None},
        {'theme': 'Y', 'avg_pct': float('nan'), 'inflow_yi': float('nan')},
    ]))
    check('缺字段 / None / NaN 不抛异常且无 "-nan"',
          '板块温度居首' in out_none and 'nan' not in out_none, out_none)
except Exception as e:
    check('缺字段 / None / NaN 不抛异常', False, f'{type(e).__name__}: {e}')

print('\n【5】板块温度排序口径透明化 + 落盘配色一致')
try:
    payload2 = rp.generate_review_payload(
        date_str='2026-09-15', top_picks=top, warnings=pd.DataFrame(),
        all_stocks=pd.DataFrame(), market={'index_close': 3888.11},
        sector_heat=HEAT_OUT, holdings_status=[], closed_today=[],
        new_positions=[], stats={'total': 0}, data_date='20260915',
    )
    t2 = payload2['markdown']['text']
    check('推送版含排序口径脚注',
          '排序口径：板块强度 = 平均涨幅×0.4 + 主力净流入×0.3 + 涨停数×0.3' in t2)
    check('推送版板块温度配色：+0.5% → 🔴 / -0.6% → 🟢',
          '🔴平均涨幅 +0.5%' in t2 and '🟢平均涨幅 -0.6%' in t2)
    with tempfile.TemporaryDirectory() as td:
        p3 = rp.save_review_report(
            date_str='2026-09-15', top_picks=top, warnings=pd.DataFrame(),
            all_stocks=pd.DataFrame(), market={'index_close': 3888.11},
            sector_heat=HEAT_OUT + [{'theme': '空值板块', 'avg_pct': None,
                                     'inflow_yi': None, 'limit_up': 0}],
            reports_dir=Path(td), holdings_status=[], closed_today=[],
            new_positions=[], stats={'total': 0}, data_date='20260915',
        )
        c3 = Path(p3).read_text(encoding='utf-8')
        check('落盘版含排序口径脚注', '排序口径：板块强度' in c3)
        check('落盘版板块温度配色与推送版一致',
              '🔴平均涨幅 +0.5%' in c3 and '🟢平均涨幅 -0.6%' in c3)
        check('落盘版数值缺失安全（None → "-"，不崩）',
              '空值板块' in c3 and '平均涨幅 -' in c3)
except Exception as e:
    check('板块温度章节生成', False, f'{type(e).__name__}: {e}')

print('\n' + '=' * 62)
print(f'📊 结果：{PASS} 通过 / {FAIL} 失败')
print('=' * 62)
sys.exit(1 if FAIL else 0)
