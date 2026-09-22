"""
飞书群机器人推送模块
=====================
把「钉钉 markdown 日报」自动转成「飞书交互卡片」并推送。

【与钉钉的三处关键差异】
  1. 签名：秒级时间戳 + sign 放在**请求体**里（钉钉是毫秒级 + 拼在 URL query）
  2. 消息体：msg_type / content（钉钉是 msgtype / markdown）
  3. Markdown：飞书卡片**不支持** # 标题 / 表格 / 引用块 → 本模块自动降级转换：
       # 一级标题 → 卡片 header 标题栏（比钉钉还好看）
       ## 二级标题 → 加粗行
       表格        → column_set 多列布局（超过 6 列时自动合并相邻最窄列）
       引用块      → 去掉 > 前缀的普通文本
       ---        → hr 分隔线

【飞书机器人配置步骤】
  1. 飞书群 → 设置 → 群机器人 → 添加机器人 → 自定义机器人
  2. 安全设置：勾选「签名校验」（推荐），或填「自定义关键词」
  3. 复制 webhook：https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx
  4. 如勾选签名校验，复制密钥（Secret）

【限制】
  - 单条消息 ≤ 30 KB
  - 自定义机器人限频 100 条/分钟
"""
import os
import re
import json
import time
import hmac
import hashlib
import base64
from typing import Dict, Tuple, List, Optional

import requests


FEISHU_API_TIMEOUT = 10          # 秒
FEISHU_MAX_BYTES = 28 * 1024     # 单卡片 JSON 上限（官方 30 KB，留 2 KB 余量）
FEISHU_MAX_ELEMENTS = 38         # 单卡片元素数上限（超了自动分片）
FEISHU_MAX_COLUMNS = 6           # 飞书 column_set 列数上限（官方限制）

# 卡片主题色：按消息类型自动选择
TEMPLATE_BY_KEYWORD = (
    ('告警', 'red'),
    ('监控', 'orange'),
    ('复盘', 'turquoise'),
    ('精选', 'blue'),
)


# ======================================================================
# 签名
# ======================================================================

def gen_sign(timestamp: str, secret: str) -> str:
    """飞书加签算法（与官方示例一致）

    签名原文 = f'{timestamp}\\n{secret}'
    以「签名原文」为 HMAC key、空字符串为 message，做 SHA256，再 base64。

    注意与钉钉的区别：
      - 钉钉：hmac(secret, f'{ts}\\n{secret}')，ts 是**毫秒**
      - 飞书：hmac(f'{ts}\\n{secret}', '')，ts 是**秒**
    """
    string_to_sign = f'{timestamp}\n{secret}'
    hmac_code = hmac.new(
        string_to_sign.encode('utf-8'), digestmod=hashlib.sha256
    ).digest()
    return base64.b64encode(hmac_code).decode('utf-8')


# ======================================================================
# markdown → 飞书卡片
# ======================================================================

def _display_width(s: str) -> int:
    """按显示宽度估算字符数（CJK 与 emoji 记 2，其余记 1）"""
    w = 0
    for ch in str(s or ''):
        o = ord(ch)
        if o > 0x1F000:            # emoji
            w += 2
        elif 0x2E80 <= o <= 0x9FFF or 0xFF00 <= o <= 0xFF60:
            w += 2                 # 中日韩文字
        else:
            w += 1
    return w


def _parse_markdown_table(block: List[str]) -> Tuple[Optional[List[str]], List[List[str]]]:
    """解析 markdown 表格块 → (表头, 数据行)

    自动识别 |---| 分隔行；各行列数不齐时右侧补空。
    """
    rows: List[List[str]] = []
    for line in block:
        line = line.strip()
        if line.startswith('|'):
            line = line[1:]
        if line.endswith('|'):
            line = line[:-1]
        rows.append([c.strip() for c in line.split('|')])

    if not rows:
        return None, []

    header = rows[0]
    data = rows[1:]
    # 第二行若是分隔线（|---|:--:|）则丢弃
    if data:
        sep = data[0]
        is_sep = len(sep) > 0 and all(
            re.fullmatch(r':?-{2,}:?', (c or '-')) for c in sep)
        if is_sep:
            data = data[1:]

    # 对齐列数
    ncol = max([len(header)] + [len(r) for r in data]) if data else len(header)
    header = (header + [''] * ncol)[:ncol]
    data = [(r + [''] * ncol)[:ncol] for r in data]
    return header, data


def _join_cells(a: str, b: str, short_width: int = 2) -> str:
    """合并两列的单元格内容。

    若其中一列内容极短（如序号 `1`），用空格并排 —— 否则「1」会单独占一行，
    使该列行数比邻列多，垂直居中后整行视觉错位（曾导致持仓表看起来"数字飘着"）。
    其余情况上下叠放，保留原列顺序，信息不丢。
    """
    a, b = (a or '').strip(), (b or '').strip()
    if not a:
        return b
    if not b:
        return a
    if _display_width(a) <= short_width or _display_width(b) <= short_width:
        return f'{a} {b}'
    return f'{a}\n{b}'


def _shrink_columns(header: List[str], data: List[List[str]],
                    max_cols: int = FEISHU_MAX_COLUMNS
                    ) -> Tuple[List[str], List[List[str]]]:
    """列数超过飞书上限时，反复合并「最窄的相邻两列」（叠放 / 并排，不丢信息）"""
    if not header:
        return header, data
    guard = 0
    while len(header) > max_cols and guard < 50:
        guard += 1
        n = len(header)
        best_i, best_w = 0, None
        for i in range(n - 1):
            w = (max([_display_width(header[i])] +
                     [_display_width(r[i]) for r in data]) +
                 max([_display_width(header[i + 1])] +
                     [_display_width(r[i + 1]) for r in data]))
            if best_w is None or w < best_w:
                best_i, best_w = i, w
        a, b = best_i, best_i + 1
        header = header[:a] + [f'{header[a]}/{header[b]}'] + header[b + 1:]
        data = [r[:a] + [_join_cells(r[a], r[b])] + r[b + 1:] for r in data]
    return header, data


def _col_weights(header: List[str], data: List[List[str]]) -> List[int]:
    """按各列最宽内容分配权重（1~4），让宽内容列拿到更多横向空间"""
    weights = []
    for i, h in enumerate(header):
        m = max([_display_width(h)] + [_display_width(r[i]) for r in data])
        if m <= 4:
            w = 1
        elif m <= 8:
            w = 2
        elif m <= 14:
            w = 3
        else:
            w = 4
        weights.append(w)
    return weights


def _make_column_set(cells: List[str], weights: List[int],
                     bg: str = 'default') -> Dict:
    """构造一行 column_set（横向多列布局）"""
    columns = []
    for cell, w in zip(cells, weights):
        columns.append({
            'tag': 'column',
            'width': 'weighted',
            'weight': w,
            'vertical_align': 'center',
            'elements': [{'tag': 'markdown', 'content': cell if cell else ' '}],
        })
    return {
        'tag': 'column_set',
        'flex_mode': 'none',
        'background_style': bg,
        'horizontal_spacing': 'small',
        'columns': columns,
    }


def _bold_cell(c) -> str:
    """表头单元格加粗。

    必须先剥掉单元格里已有的 ** 包裹，否则 `**60日**` 会被二次加粗成
    `****60日****`，在飞书里会渲染成带星号的乱码。
    列合并后还会出现 `**60日**/主力净额` 这种混合串，所以用整体替换而不是首尾判断。
    """
    s = str(c or '').replace('**', '').strip()
    return f'**{s}**' if s else ' '


def _table_to_elements(header: Optional[List[str]],
                       data: List[List[str]]) -> List[Dict]:
    """表格 → 一组 column_set 元素（表头灰底加粗 + 数据行交替斑马纹）"""
    if not header:
        return []
    header, data = _shrink_columns(header, data)
    weights = _col_weights(header, data)

    elements = [_make_column_set(
        [_bold_cell(c) for c in header], weights, 'grey')]
    for idx, row in enumerate(data):
        bg = 'default' if idx % 2 == 0 else 'grey'
        elements.append(_make_column_set(
            [str(c) if c else ' ' for c in row], weights, bg))
    return elements


def _pick_template(title: str) -> str:
    for kw, tpl in TEMPLATE_BY_KEYWORD:
        if kw in (title or ''):
            return tpl
    return 'blue'


def markdown_to_feishu_card(text: str, title: str,
                            template: str = '') -> Dict:
    """把钉钉风格的 markdown 正文转成飞书交互卡片

    转换规则见模块顶部说明。连续的普通文本行会合并进同一个 markdown 元素，
    避免元素数量爆炸。
    """
    elements: List[Dict] = []
    buf: List[str] = []

    def flush():
        if buf:
            content = '\n'.join(buf).strip('\n')
            if content.strip():
                elements.append({'tag': 'markdown', 'content': content})
            buf.clear()

    lines = (text or '').split('\n')

    # 正文首个一级标题 → 升级为卡片标题栏文字。
    # 否则会与 header 标题重复（如 header「📊 主升浪复盘」+ 正文「📊 主升浪盘后复盘」）。
    card_title = title
    pending_h1 = False
    for _l in lines:
        _s = _l.strip()
        if not _s:
            continue
        _m = re.match(r'^#\s+(.*)$', _s)
        if _m:
            card_title = _m.group(1).strip()
            pending_h1 = True
        break

    i = 0
    while i < len(lines):
        raw = lines[i]
        s = raw.strip()

        # ---- 表格块 ----
        if s.startswith('|') and s.endswith('|'):
            flush()
            block = []
            while i < len(lines) and lines[i].strip().startswith('|'):
                block.append(lines[i])
                i += 1
            header, data = _parse_markdown_table(block)
            elements.extend(_table_to_elements(header, data))
            continue

        # ---- 分隔线 ----
        if s in ('---', '***', '___') or re.fullmatch(r'-{3,}', s or ''):
            flush()
            elements.append({'tag': 'hr'})
            i += 1
            continue

        # ---- 标题（# / ## / ###）→ 加粗行 ----
        m = re.match(r'^(#{1,6})\s+(.*)$', s)
        if m:
            flush()
            if pending_h1 and len(m.group(1)) == 1:
                # 已提升为卡片标题栏，正文不再重复显示
                pending_h1 = False
                i += 1
                continue
            elements.append({'tag': 'markdown',
                             'content': f"**{m.group(2).strip()}**"})
            i += 1
            continue

        # ---- 引用块 → 去掉 > 前缀 ----
        if s.startswith('>'):
            content = s.lstrip('>').strip()
            buf.append(content)
            i += 1
            continue

        # ---- 列表项 → • 前缀（飞书不渲染 markdown 列表）----
        m = re.match(r'^[-*+]\s+(.*)$', s)
        if m:
            buf.append(f"• {m.group(1)}")
            i += 1
            continue

        buf.append(raw)
        i += 1

    flush()

    if not elements:
        elements = [{'tag': 'markdown', 'content': '（无内容）'}]

    card = {
        'config': {'wide_screen_mode': True},
        'header': {
            'template': template or _pick_template(card_title),
            'title': {'tag': 'plain_text', 'content': card_title or '主升浪日报'},
        },
        'elements': elements,
    }
    return card


# ======================================================================
# 分片 / 推送
# ======================================================================

def _json_size(obj) -> int:
    return len(json.dumps(obj, ensure_ascii=False).encode('utf-8'))


def _split_card(card: Dict) -> List[Dict]:
    """卡片过大时按元素切分成多张（标题只保留在第一张）"""
    elements = card.get('elements', [])
    if (len(elements) <= FEISHU_MAX_ELEMENTS
            and _json_size(card) <= FEISHU_MAX_BYTES):
        return [card]

    chunks: List[List[Dict]] = []
    cur: List[Dict] = []
    cur_size = 0
    for el in elements:
        esz = _json_size(el)
        if cur and (len(cur) >= FEISHU_MAX_ELEMENTS
                    or cur_size + esz > FEISHU_MAX_BYTES - 1024):
            chunks.append(cur)
            cur, cur_size = [], 0
        cur.append(el)
        cur_size += esz
    if cur:
        chunks.append(cur)

    base_title = card.get('header', {}).get('title', {}).get('content', '日报')
    base_tpl = card.get('header', {}).get('template', 'blue')
    out = []
    for idx, chunk in enumerate(chunks):
        title = base_title if idx == 0 else f'{base_title}（{idx + 1}/{len(chunks)}）'
        out.append({
            'config': card.get('config', {'wide_screen_mode': True}),
            'header': {'template': base_tpl,
                       'title': {'tag': 'plain_text', 'content': title}},
            'elements': chunk,
        })
    return out


def push_card(webhook: str, card: Dict, secret: str = '') -> Tuple[bool, str]:
    """推送一张飞书交互卡片

    参数:
        webhook: 飞书 webhook（https://open.feishu.cn/open-apis/bot/v2/hook/xxx）
        card: 卡片 JSON
        secret: 加签密钥（可选）

    返回:
        (success, message)
    """
    if not webhook:
        return False, '未配置飞书 webhook'

    body: Dict = {'msg_type': 'interactive', 'card': card}
    if secret:
        try:
            ts = str(int(time.time()))          # 飞书用秒级时间戳
            body['timestamp'] = ts
            body['sign'] = gen_sign(ts, secret)
        except Exception as e:
            return False, f'飞书加签失败: {e}'

    size = _json_size(body)
    if size > 30 * 1024:
        return False, f'飞书卡片过大（{size} bytes > 30 KB）'

    try:
        resp = requests.post(
            webhook,
            json=body,
            headers={'Content-Type': 'application/json; charset=utf-8'},
            timeout=FEISHU_API_TIMEOUT,
        )
        resp.raise_for_status()
        try:
            result = resp.json()
        except Exception:
            return False, f'飞书返回非 JSON：{resp.text[:120]}'

        # 新版：{"code":0,"msg":"success"}；旧版：{"StatusCode":0,"StatusMessage":"success"}
        code = result.get('code')
        if code is None:
            code = result.get('StatusCode')
        if code == 0:
            return True, '推送成功'
        return False, (f"飞书返回错误: {result.get('msg') or result.get('StatusMessage')} "
                       f"(code={code})")

    except requests.exceptions.Timeout:
        return False, '飞书请求超时'
    except requests.exceptions.RequestException as e:
        return False, f'飞书网络异常: {e}'
    except Exception as e:
        return False, f'飞书推送失败: {e}'


def push_to_feishu(webhook: str, payload: Dict, secret: str = '',
                   template: str = '') -> Tuple[bool, str]:
    """推送钉钉格式的 payload 到飞书（内部自动转卡片 + 自动分片）

    参数:
        webhook: 飞书 webhook URL
        payload: 钉钉格式 {"msgtype":"markdown","markdown":{"title":..,"text":..}}
                 也兼容已转好的飞书卡片 {"msg_type":"interactive","card":{...}}
        secret:  加签密钥
        template: 卡片主题色（留空则按标题关键词自动选择）
    """
    if not webhook:
        return False, '未配置飞书 webhook'

    # 兼容直接传卡片
    if isinstance(payload, dict) and 'card' in payload:
        cards = _split_card(payload['card'])
    else:
        md = (payload or {}).get('markdown', {}) or {}
        title = md.get('title', '主升浪日报')
        text = md.get('text', '')
        card = markdown_to_feishu_card(text, title, template)
        cards = _split_card(card)

    ok_all = True
    msgs = []
    for idx, c in enumerate(cards):
        ok, msg = push_card(webhook, c, secret)
        msgs.append(msg if len(cards) == 1 else f'[第{idx + 1}片] {msg}')
        if not ok:
            ok_all = False
            break
        if idx < len(cards) - 1:
            time.sleep(1)      # 分片间稍作间隔，降低触发限频的概率
    return ok_all, '; '.join(msgs)


# ======================================================================
# 本地测试入口
# ======================================================================

def main():
    webhook = os.environ.get('FEISHU_WEBHOOK', '').strip()
    secret = os.environ.get('FEISHU_SECRET', '').strip()

    if not webhook:
        print('❌ 请先设置 FEISHU_WEBHOOK 环境变量')
        return

    demo = {
        'msgtype': 'markdown',
        'markdown': {
            'title': '📊 主升浪精选 - 飞书推送测试',
            'text': """# 🎯 飞书推送链路测试

> 📡 数据源：Tushare
> 🕐 生成时间：刚刚

## 📋 TOP 2 精选

| # | 代码 | 名称 | 现价 | 当日 | **60日** | 超大单占比 | 评分 | 关键 |
|---|---|---|---|---|---|---|---|---|
| 🥇 | 600519 | 贵州茅台 | 1500.00 | 🟥 +3.21% | **+25.3%** | +4.2% | 85 | 白酒 |
| 🥈 | 000858 | 五粮液 | 128.50 | 🟥 +2.10% | **+18.7%** | +2.6% | 82 | 白酒 |

## ⚡ 操作指引

- **🥇 贵州茅台** 回踩 5 日线可低吸
- **🥈 五粮液** 等待放量突破

---

⚠️ **免责声明**：以上内容由 AI 生成，仅供参考，不构成投资建议。
"""
        }
    }
    ok, msg = push_to_feishu(webhook, demo, secret)
    print(f"{'✅' if ok else '❌'} {msg}")


if __name__ == '__main__':
    main()
