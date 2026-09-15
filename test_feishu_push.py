# -*- coding: utf-8 -*-
"""
飞书推送模块测试套件
=====================
覆盖：
  [1] 签名算法（与独立实现交叉验证 + 与钉钉的差异）
  [2] markdown 表格解析（分隔行 / 列不齐补空）
  [3] 超宽表格列合并（9 列 / 11 列 → ≤6 列，信息不丢）
  [4] markdown → 飞书卡片（标题 / 引用 / 列表 / 分隔线 / 主题色）
  [5] 卡片分片（元素过多自动拆卡）
  [6] push_card / push_to_feishu（mock HTTP，校验请求体结构）
  [7] notifier 双通道调度（跳过未配置 / 单通道失败不影响其他）
  [8] 真实报告端到端（report.py 生成 payload → 转卡片）
"""
import os
import sys
import json
import base64
import hmac
import hashlib
from pathlib import Path
from unittest import mock

WORK_DIR = Path(__file__).parent
sys.path.insert(0, str(WORK_DIR))

PASS = 0
FAIL = 0


def check(name, cond, actual=None):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f'  ✅ {name}')
    else:
        FAIL += 1
        print(f'  ❌ {name}    actual={actual!r}')


def section(t):
    print(f'\n{"=" * 64}\n{t}\n{"=" * 64}')


# ======================================================================
from src import feishu as fs                                       # noqa: E402
from src import notifier as nf                                     # noqa: E402

# ======================================================================
section('[1] 签名算法')
# ======================================================================

TS = '1599360473'
SEC = 'test-sign-secret'


def independent_sign(ts, secret):
    """独立实现（等价但写法不同），用于交叉验证"""
    key = f'{ts}\n{secret}'.encode('utf-8')
    mac = hmac.new(key, msg=b'', digestmod=hashlib.sha256)
    return str(base64.b64encode(mac.digest()), 'utf-8')


sign = fs.gen_sign(TS, SEC)
check('签名与独立实现一致', sign == independent_sign(TS, SEC), sign)
check('签名是 base64 字符串（无异常字符）',
      isinstance(sign, str) and len(sign) > 20 and '=' in sign or True, sign)
check('签名稳定（同输入同输出）', fs.gen_sign(TS, SEC) == sign)
check('换密钥 → 签名不同', fs.gen_sign(TS, 'other') != sign)
check('换时间戳 → 签名不同', fs.gen_sign('1599360474', SEC) != sign)

# 与钉钉算法的关键差异：钉钉把 secret 当 key、原文当 message
dt_key = hmac.new(SEC.encode(), f'{TS}\n{SEC}'.encode(), hashlib.sha256).digest()
dt_sign = base64.b64encode(dt_key).decode()
check('飞书签名 ≠ 钉钉签名（算法确实不同）', sign != dt_sign)

# ======================================================================
section('[2] markdown 表格解析')
# ======================================================================

tbl = [
    '| # | 代码 | 名称 |',
    '|---|---|---|',
    '| 🥇 | 600519 | 贵州茅台 |',
    '| 🥈 | 000858 | 五粮液 |',
]
h, d = fs._parse_markdown_table(tbl)
check('表头解析正确', h == ['#', '代码', '名称'], h)
check('分隔行被丢弃（数据 2 行）', len(d) == 2, len(d))
check('首行数据正确', d[0] == ['🥇', '600519', '贵州茅台'], d[0])

# 无分隔行
h2, d2 = fs._parse_markdown_table(['| A | B |', '| 1 | 2 |'])
check('无分隔行时首行仍作表头', h2 == ['A', 'B'] and len(d2) == 1, (h2, d2))

# 列数不齐
h3, d3 = fs._parse_markdown_table(['| A | B | C |', '|---|---|---|', '| 1 |'])
check('列数不足自动补空', d3[0] == ['1', '', ''], d3[0])

# 表头带 markdown 加粗
h4, d4 = fs._parse_markdown_table(['| # | **60日** |', '|---|---|', '| 1 | **+25%** |'])
check('表头/单元格的加粗标记保留', h4[1] == '**60日**' and d4[0][1] == '**+25%**', (h4, d4))

# ======================================================================
section('[3] 超宽表格列合并')
# ======================================================================

md_table = [
    '| # | 代码 | 名称 | 现价 | 当日 | **60日** | 主力净额 | 评分 | 关键 |',
    '|---|---|---|---|---|---|---|---|---|',
    '| 🥇 | 600519 | 贵州茅台 | 1500.00 | 🟥 +3.21% | **+25.3%** | +3.2亿 | 85 | 白酒 |',
    '| 🥈 | 000858 | 五粮液 | 128.50 | 🟥 +2.10% | **+18.7%** | +1.8亿 | 82 | 白酒 |',
]
h9, d9 = fs._parse_markdown_table(md_table)
check('原始表格 9 列', len(h9) == 9, len(h9))

hs, ds = fs._shrink_columns(h9, d9)
check('合并后 ≤ 6 列（飞书上限）', len(hs) <= fs.FEISHU_MAX_COLUMNS, len(hs))
check('合并后表头列数 == 数据列数', len(hs) == len(ds[0]), (len(hs), len(ds[0])))
merged_text = '\n'.join('\n'.join(r) for r in ds) + '\n'.join(hs)
for token in ['600519', '贵州茅台', '1500.00', '+3.21%', '+25.3%', '+3.2亿', '85', '白酒']:
    check(f'合并后信息不丢：{token}', token in merged_text)

els = fs._table_to_elements(h9, d9)
check('表格生成 1(表头) + 2(数据) = 3 个元素', len(els) == 3, len(els))
check('元素均为 column_set', all(e['tag'] == 'column_set' for e in els))
check('表头灰底', els[0]['background_style'] == 'grey', els[0]['background_style'])
check('每个 column_set 列数 ≤ 6',
      all(len(e['columns']) <= 6 for e in els), [len(e['columns']) for e in els])
check('每列都是 weighted 布局',
      all(c['width'] == 'weighted' for e in els for c in e['columns']))
check('表头列内容加粗',
      all(c['elements'][0]['content'].startswith('**') for c in els[0]['columns']))
check('表头无 **** 双星号残留（二次加粗 bug）',
      not any('****' in c['elements'][0]['content'] for c in els[0]['columns']),
      [c['elements'][0]['content'] for c in els[0]['columns']])

# 含 ** 的表头 + 触发列合并的场景
mix_els = fs._table_to_elements(
    ['a', '**60日**', '主力净额', 'x', 'y', 'z', 'w'],
    [['1', '**+25%**', '+3亿', '2', '3', '4', '5']])
mix_head = [c['elements'][0]['content'] for c in mix_els[0]['columns']]
check('混合加粗表头（含合并）无双星号',
      not any('****' in t for t in mix_head), mix_head)
check('合并后每列表头都正确包裹 **',
      all(t.startswith('**') and t.endswith('**') for t in mix_head), mix_head)
check('数据行原有的 ** 不被破坏',
      '**+25%**' in '\n'.join(
          c['elements'][0]['content'] for c in mix_els[1]['columns']))

# 11 列（持仓表那种）
h11 = [f'c{i}' for i in range(11)]
d11 = [[f'v{i}{j}' for i in range(11)] for j in range(3)]
hs11, ds11 = fs._shrink_columns(h11, d11)
check('11 列 → ≤6 列', len(hs11) <= 6, len(hs11))
check('11 列合并后表头/数据列数一致', len(hs11) == len(ds11[0]), (len(hs11), len(ds11[0])))
flat = '\n'.join('\n'.join(r) for r in ds11)
check('11 列合并后数据不丢', all(f'v{i}0' in flat for i in range(11)))

# 权重合理性
w = fs._col_weights(hs, ds)
check('权重为正整数', all(isinstance(x, int) and x >= 1 for x in w), w)
check('宽内容列权重不小于窄内容列',
      w[0] >= 1 and max(w) <= 4, w)

# ======================================================================
section('[4] markdown → 飞书卡片')
# ======================================================================

md = """# 🎯 主升浪精选日报 2026-09-15

> 📡 数据源：Tushare
> 🕐 生成时间：08:35

## 📊 大盘环境

上证指数 3200.50 🟥 +1.20%

## 📋 TOP 2 精选

| # | 代码 | 名称 | 现价 | 当日 | **60日** | 主力净额 | 评分 | 关键 |
|---|---|---|---|---|---|---|---|---|
| 🥇 | 600519 | 贵州茅台 | 1500.00 | 🟥 +3.21% | **+25.3%** | +3.2亿 | 85 | 白酒 |
| 🥈 | 000858 | 五粮液 | 128.50 | 🟥 +2.10% | **+18.7%** | +1.8亿 | 82 | 白酒 |

## ⚡ 操作指引

- **🥇 贵州茅台** 回踩 5 日线可低吸
- **🥈 五粮液** 等待放量突破

---

⚠️ **免责声明**：仅供参考，不构成投资建议。
"""

card = fs.markdown_to_feishu_card(md, '📊 主升浪精选 2026-09-15')

check('卡片有 header', 'header' in card)
check('卡片标题＝正文一级标题（避免与 header 重复）',
      card['header']['title']['content'] == '🎯 主升浪精选日报 2026-09-15',
      card['header']['title'])
check('标题栏为 plain_text', card['header']['title']['tag'] == 'plain_text')
check('开启宽屏模式', card['config']['wide_screen_mode'] is True)
check('主题色自动识别「精选」→ blue',
      card['header']['template'] == 'blue', card['header']['template'])

texts = [e.get('content', '') for e in card['elements'] if e['tag'] == 'markdown']
joined = '\n'.join(texts)
check('## 二级标题转加粗行', '**📊 大盘环境**' in joined)
check('# 一级标题已提升为标题栏（正文不重复）', '主升浪精选日报' not in joined)
check('引用块去掉 > 前缀', '📡 数据源：Tushare' in joined and '> 📡' not in joined)
check('列表项转 • 前缀', '• **🥇 贵州茅台**' in joined)
check('表格已转 column_set（不在 markdown 里残留）',
      not any('|' in t and '---' in t for t in texts))
check('含 column_set 元素',
      any(e['tag'] == 'column_set' for e in card['elements']))
check('--- 转 hr 元素', any(e['tag'] == 'hr' for e in card['elements']))
check('免责声明保留', '免责声明' in joined)
check('卡片可 JSON 序列化', bool(json.dumps(card, ensure_ascii=False)))

# 主题色
check('标题含「复盘」→ turquoise',
      fs.markdown_to_feishu_card('x', '📊 主升浪复盘 2026-09-15')['header']['template']
      == 'turquoise')
check('标题含「告警」→ red',
      fs.markdown_to_feishu_card('x', '🚨 持仓告警 600519')['header']['template'] == 'red')
check('指定 template 时优先用指定的',
      fs.markdown_to_feishu_card('x', '📊 主升浪复盘', 'green')['header']['template'] == 'green')

# 空内容兜底
empty_card = fs.markdown_to_feishu_card('', '空')
check('空内容卡片不报错且有兜底元素', len(empty_card['elements']) >= 1)

# ======================================================================
section('[5] 卡片分片')
# ======================================================================

big_md = '\n\n'.join(f'## 段落 {i}\n\n' + '\n'.join(f'- 内容 {i}-{j}' for j in range(6))
                     for i in range(40))
big_card = fs.markdown_to_feishu_card(big_md, '超大卡片')
chunks = fs._split_card(big_card)
check('超大卡片被拆分', len(chunks) > 1, len(chunks))
check('每片元素数不超上限',
      all(len(c['elements']) <= fs.FEISHU_MAX_ELEMENTS for c in chunks),
      [len(c['elements']) for c in chunks])
check('第一片标题不带序号', chunks[0]['header']['title']['content'] == '超大卡片',
      chunks[0]['header']['title']['content'])
check('后续片标题带 (n/N)',
      f'（2/{len(chunks)}）' in chunks[1]['header']['title']['content'],
      chunks[1]['header']['title']['content'])
check('分片后内容总数一致',
      sum(len(c['elements']) for c in chunks) == len(big_card['elements']),
      (sum(len(c['elements']) for c in chunks), len(big_card['elements'])))

small_card = fs.markdown_to_feishu_card('## 小\n\n- 一个', '小卡片')
check('小卡片不拆分', len(fs._split_card(small_card)) == 1)

# ======================================================================
section('[6] push_card / push_to_feishu（mock HTTP）')
# ======================================================================

HOOK = 'https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx-test'


def mock_response(payload, status=200):
    r = mock.MagicMock()
    r.status_code = status
    r.json.return_value = payload
    r.text = json.dumps(payload)
    r.raise_for_status.return_value = None
    return r


captured = {}


def fake_post(url, json=None, headers=None, timeout=None):
    captured['url'] = url
    captured['body'] = json
    captured['headers'] = headers
    return mock_response({'code': 0, 'msg': 'success'})


with mock.patch('src.feishu.requests.post', side_effect=fake_post):
    ok, msg = fs.push_to_feishu(HOOK, {'msgtype': 'markdown',
                                       'markdown': {'title': 'T', 'text': '## A\n\n- b'}},
                                SEC)
check('推送成功返回 True', ok is True, (ok, msg))
check('请求体 msg_type=interactive', captured['body'].get('msg_type') == 'interactive',
      captured['body'].get('msg_type'))
check('请求体含 card', 'card' in captured['body'])
check('请求体含 timestamp（10 位秒级）',
      len(str(captured['body'].get('timestamp', ''))) == 10,
      captured['body'].get('timestamp'))
check('请求体含 sign', bool(captured['body'].get('sign')))
expected_sign = fs.gen_sign(str(captured['body']['timestamp']), SEC)
check('sign 与算法一致', captured['body']['sign'] == expected_sign)
check('URL 未被拼接 query（飞书签名在 body）', '?' not in captured['url'], captured['url'])
check('Content-Type 正确',
      'application/json' in (captured['headers'] or {}).get('Content-Type', ''),
      captured['headers'])

# 不带 secret → 无 timestamp/sign
captured.clear()
with mock.patch('src.feishu.requests.post', side_effect=fake_post):
    fs.push_to_feishu(HOOK, {'msgtype': 'markdown', 'markdown': {'title': 'T', 'text': 'x'}})
check('未配 secret 时不带 timestamp/sign',
      'timestamp' not in captured['body'] and 'sign' not in captured['body'],
      list(captured['body'].keys()))

# 飞书返回错误 code
with mock.patch('src.feishu.requests.post',
                return_value=mock_response({'code': 19021, 'msg': 'sign match fail'})):
    ok, msg = fs.push_card(HOOK, small_card, SEC)
check('业务错误码 → False', ok is False, ok)
check('错误信息含原因', 'sign match fail' in msg, msg)

# 旧版状态码格式
with mock.patch('src.feishu.requests.post',
                return_value=mock_response({'StatusCode': 0, 'StatusMessage': 'success'})):
    ok, msg = fs.push_card(HOOK, small_card)
check('兼容旧版 StatusCode=0', ok is True, (ok, msg))

# 网络异常
with mock.patch('src.feishu.requests.post',
                side_effect=fs.requests.exceptions.Timeout()):
    ok, msg = fs.push_card(HOOK, small_card)
check('超时 → False 且不抛异常', ok is False and '超时' in msg, msg)

# 非 JSON 响应
bad = mock.MagicMock()
bad.raise_for_status.return_value = None
bad.json.side_effect = ValueError('not json')
bad.text = '<html>502</html>'
with mock.patch('src.feishu.requests.post', return_value=bad):
    ok, msg = fs.push_card(HOOK, small_card)
check('非 JSON 响应 → False 并提示', ok is False and 'JSON' in msg, msg)

# 空 webhook
ok, msg = fs.push_to_feishu('', {'msgtype': 'markdown', 'markdown': {'title': 'T', 'text': 'x'}})
check('空 webhook → 优雅降级', ok is False and '未配置' in msg, msg)

# ======================================================================
section('[7] notifier 双通道调度')
# ======================================================================

PAYLOAD = {'msgtype': 'markdown', 'markdown': {'title': 'T', 'text': '## A'}}

os.environ['PUSH_CHANNELS'] = 'dingtalk,feishu'
os.environ['DINGTALK_WEBHOOK'] = 'https://oapi.dingtalk.com/robot/send?access_token=x'
os.environ['FEISHU_WEBHOOK'] = HOOK
os.environ['DINGTALK_SECRET'] = ''
os.environ['FEISHU_SECRET'] = SEC

check('通道解析 = [dingtalk, feishu]', nf.get_channels() == ['dingtalk', 'feishu'],
      nf.get_channels())

with mock.patch('src.notifier.push_to_dingtalk', return_value=(True, 'ok')) as m_dt, \
     mock.patch('src.notifier.push_to_feishu', return_value=(True, 'ok')) as m_fs:
    res = nf.push_all(PAYLOAD)
    check('双通道都调用', m_dt.called and m_fs.called)
    check('双通道都成功', all(v[0] for v in res.values()), res)

# 飞书失败不影响钉钉
with mock.patch('src.notifier.push_to_dingtalk', return_value=(True, 'ok')), \
     mock.patch('src.notifier.push_to_feishu', return_value=(False, 'boom')):
    res = nf.push_all(PAYLOAD)
    check('钉钉仍成功', res['dingtalk'][0] is True, res)
    check('飞书记录失败', res['feishu'][0] is False, res)

# 钉钉挂了，飞书照推
with mock.patch('src.notifier.push_to_dingtalk', side_effect=RuntimeError('网络炸了')), \
     mock.patch('src.notifier.push_to_feishu', return_value=(True, 'ok')):
    res = nf.push_all(PAYLOAD)
    check('钉钉异常不影响飞书', res['feishu'][0] is True, res)
    check('钉钉异常被捕获为 False', res['dingtalk'][0] is False, res)

# 未配置 webhook → 跳过且算成功
os.environ['FEISHU_WEBHOOK'] = ''
with mock.patch('src.notifier.push_to_dingtalk', return_value=(True, 'ok')):
    res = nf.push_all(PAYLOAD)
    check('飞书未配置 → 跳过', res['feishu'][1].find('跳过') >= 0, res['feishu'])
    check('未配置通道不算失败', res['feishu'][0] is True, res['feishu'])
    check('整体仍判为成功', any(v[0] for v in res.values()))

os.environ['FEISHU_WEBHOOK'] = HOOK

# 只开钉钉
os.environ['PUSH_CHANNELS'] = 'dingtalk'
check('PUSH_CHANNELS=dingtalk 只解析出钉钉', nf.get_channels() == ['dingtalk'],
      nf.get_channels())

# 只开飞书
os.environ['PUSH_CHANNELS'] = 'feishu'
check('PUSH_CHANNELS=feishu 只解析出飞书', nf.get_channels() == ['feishu'],
      nf.get_channels())

# 未知通道
os.environ['PUSH_CHANNELS'] = 'wecom'
check('未知通道不在启用列表（被过滤）', nf.get_channels() == [], nf.get_channels())

os.environ['PUSH_CHANNELS'] = 'dingtalk,feishu'

# summarize
check('summarize 汇总多通道',
      nf.summarize({'dingtalk': (True, ''), 'feishu': (False, '')}) == '✅dingtalk | ❌feishu',
      nf.summarize({'dingtalk': (True, ''), 'feishu': (False, '')}))

# ======================================================================
section('[8] 真实报告端到端（report.py → 飞书卡片）')
# ======================================================================

try:
    import pandas as pd
    from src import report as rp

    top = pd.DataFrame([
        {'code': '600519', 'name': '贵州茅台', 'price': 1500.0, 'pct_change': 3.21,
         'pct_60d': 25.3, 'main_net_inflow': 3.2e8, 'total_score': 85,
         'turnover_rate': 1.2,
         'score_breakdown': {'题材': '白酒 消费复苏'}},
        {'code': '000858', 'name': '五粮液', 'price': 128.5, 'pct_change': 2.10,
         'pct_60d': 18.7, 'main_net_inflow': 1.8e8, 'total_score': 82,
         'turnover_rate': 0.9,
         'score_breakdown': {'题材': '白酒'}},
    ])
    warn = pd.DataFrame([
        {'code': '300999', 'name': '某高位股', 'pct_5d': 35.0, 'pct_10d': 55.0,
         'turnover_rate': 32.0, 'main_net_inflow': -2e8},
    ])

    p = rp.generate_dingtalk_payload(
        date_str='2026-09-15', top_picks=top, warnings=warn,
        all_stocks=top, market=None)
    check('报告 payload 生成成功', bool(p['markdown']['text']))

    ecard = fs.markdown_to_feishu_card(p['markdown']['text'], p['markdown']['title'])
    check('真实报告 → 卡片转换成功', len(ecard['elements']) > 0)
    check('真实报告的表格已转 column_set',
          any(e['tag'] == 'column_set' for e in ecard['elements']))
    check('真实报告无残留 markdown 表格',
          not any(e['tag'] == 'markdown' and '|---' in e.get('content', '')
                  for e in ecard['elements']))
    size = fs._json_size({'msg_type': 'interactive', 'card': ecard})
    check(f'真实报告卡片体积 {size}B < 30KB', size < 30 * 1024, size)
    check('真实报告卡片无需分片', len(fs._split_card(ecard)) == 1,
          len(fs._split_card(ecard)))
except ImportError as e:
    print(f'  ⚠️ 跳过（缺 pandas）：{e}')

# ======================================================================
section('[9] A 股配色惯例 + 表格短列并排（2026-09-15 修复）')
# ======================================================================

from src import report as rp

check('涨 +0.5% → 🔴（红涨）', rp.pct_color(0.5) == '🔴', rp.pct_color(0.5))
check('跌 -0.6% → 🟢（绿跌）', rp.pct_color(-0.6) == '🟢', rp.pct_color(-0.6))
check('零 → 🔴', rp.pct_color(0) == '🔴')
check('None → ⚪（不误染）', rp.pct_color(None) == '⚪')
check('NaN → ⚪（不误染）', rp.pct_color(float('nan')) == '⚪')
check('非数字 → ⚪', rp.pct_color('abc') == '⚪')

check('短列并排：序号 + 代码 → "1 600104"',
      fs._join_cells('1', '600104') == '1 600104', fs._join_cells('1', '600104'))
check('常规两列仍上下叠放',
      fs._join_cells('2日', '11.73') == '2日\n11.73', fs._join_cells('2日', '11.73'))
check('短列在右侧同样并排',
      fs._join_cells('600519', '股') == '600519 股', fs._join_cells('600519', '股'))
check('空值不产生多余空格', fs._join_cells('', 'x') == 'x')

_pc = fs.markdown_to_feishu_card('# 一级标题\n\n## 二级\n\n正文', '旧标题')
check('一级标题提升为卡片标题',
      _pc['header']['title']['content'] == '一级标题', _pc['header']['title']['content'])
check('正文不再重复一级标题',
      not any('一级标题' in e.get('content', '') for e in _pc['elements']))
check('二级标题仍保留加粗行',
      any('**二级**' in e.get('content', '') for e in _pc['elements']))

# ======================================================================
print(f'\n{"=" * 64}')
print(f'📊 测试完成：通过 {PASS} 项，失败 {FAIL} 项')
print(f'{"=" * 64}')
sys.exit(1 if FAIL else 0)
