# 🎯 主升浪日报 · GitHub Actions 版（钉钉群推送）

> 完全云端执行 / 零月费 / 永久免费 — 每天 08:45 自动推送主升浪精选 TOP 5 到您的钉钉群

## ✨ 项目亮点

- ✅ **完全云端**：GitHub Actions 跑在微软 Azure 云端，电脑关机 100% 不影响
- ✅ **零月费**：GitHub 每月送 2000 分钟 Actions 运行时间，本项目每天只需 1-2 分钟
- ✅ **永久免费**：无过期、无续费、无服务费
- ✅ **每日推送**：每个交易日（周一至周五）北京时间 08:45 自动跑
- ✅ **手动触发**：在 GitHub 网页可随时手动跑一次（用于测试）
- ✅ **历史归档**：每次报告自动 commit 到 GitHub 仓库，可追溯
- ✅ **钉钉直达**：通过钉钉群自定义 webhook 机器人推送，手机端实时收到消息

---

## 📋 准备工作（3 分钟）

### 1. 注册 GitHub 账号（已有跳过）

访问 https://github.com/signup 注册免费账号。

### 2. 在钉钉群里添加自定义 webhook 机器人

1. 手机打开 **钉钉** → 进入您已建好的群
2. 右上角 **「···」→「群机器人」→「添加」→「自定义」**
3. 机器人名：`主升浪日报`
4. **安全设置** 推荐选 **「加签」**（更安全）：
   - 勾选「加签」后会自动生成一个密钥（`SEC...` 开头），**复制下来**
   - 如果您不方便复制加签，可选 **「自定义关键词」** 并填 **`主升浪`**（推送内容里必须包含这个词）
5. 勾选协议 → **复制 webhook URL**（形如 `https://oapi.dingtalk.com/robot/send?access_token=xxx`）

> 💡 **加签 vs 自定义关键词**：
> - 加签（推荐）：webhook URL + 时间戳 + 签名验证，最安全
> - 自定义关键词：内容里必须出现「主升浪」三个字，否则推送失败
> - 二选一即可，本项目两种模式都支持

---

## 🚀 部署步骤（10 分钟）

### 第 1 步：上传代码到您自己的 GitHub 仓库

1. 在 GitHub 网页 **「New repository」** 创建新仓库，例如 `daily-stock-selector`
2. 把本目录的 **所有文件** 上传（拖拽到网页即可）
3. ⚠️ 路径必须保留 `.github/workflows/daily.yml`（GitHub 才能识别为 workflow）

### 第 2 步：配置 Secrets

进入您创建的仓库页面：
1. **Settings** → **Secrets and variables** → **Actions**
2. 点 **「New repository secret」** 添加：

| Secret 名 | 值 | 必填 |
|---|---|---|
| `DINGTALK_WEBHOOK` | 钉钉 webhook URL | ✅ 必填 |
| `DINGTALK_SECRET` | 加签密钥（`SEC...`）| ⚠️ 可选 |

> ⚠️ **如果选择加签模式**：`DINGTALK_SECRET` 必须填；自定义关键词模式可不填。

### 第 3 步：启用 Actions

1. 进入仓库 **Actions** 页面
2. 如果看到「Workflows aren't being run on this forked repository」提示，点 **「I understand my workflows, go ahead and enable them」**

### 第 4 步：手动测试一次（不实际推送）

1. 进入 **Actions** → 左侧选 **「主升浪精选日报」**
2. 右侧点 **「Run workflow」** → 默认勾选 **「仅测试，不推钉钉」** → 点绿色按钮
3. 等待 1-2 分钟，看运行日志（应该是 ✅ 成功，但不真正推送）

### 第 5 步：正式推送测试

重复第 4 步，但**取消勾选**「仅测试」，真正推送一次。
**打开钉钉群看是否收到主升浪日报消息。**

---

## ⏰ 触发时间说明

GitHub Actions 的 cron 使用 **UTC 时间**（协调世界时）。

```
北京时间 = UTC + 8 小时
08:45 北京时间 = 00:45 UTC
```

workflow 文件中已配置：`cron: '45 0 * * 1-5'`
- `45` = 第 45 分钟
- `0` = 0 点（UTC）
- `* * * 1-5` = 每月任意日、周一至周五

> ⚠️ **注意**：GitHub 的 cron 调度通常有 5-30 分钟延迟（不是精确到分钟）。实际上可能在 08:45-09:15 之间触发。这是 GitHub 平台的限制，不是本项目的问题。

---

## 📂 项目结构

```
daily-stock-selector/
├── .github/workflows/daily.yml    # GitHub Actions 配置
├── src/
│   ├── __init__.py
│   ├── config.py                   # 配置（环境变量）
│   ├── data_fetcher.py             # akshare 数据抓取
│   ├── selector.py                  # 五维评分筛选
│   ├── report.py                    # 报告生成
│   └── dingtalk.py                  # 钉钉推送（加签/普通双模式）
├── main.py                          # 主入口
├── requirements.txt                 # Python 依赖
├── README.md                        # 本文档
└── reports/                         # 历史报告（自动生成）
    ├── 主升浪精选_2026-09-07.md
    ├── 主升浪精选_2026-09-08.md
    └── ...
```

---

## 🎯 评分维度（满分 100）

| 维度 | 权重 | 评分要点 |
|---|---|---|
| **技术面** | 25% | 当日涨幅（温和上涨加分）、5日涨幅、量比 |
| **资金面** | 20% | 主力净流入绝对值、换手率合理性 |
| **估值** | 15% | PE-TTM（亏损 0 分）、PB（破净加分）|
| **题材催化** | 15% | 预定义热门主题（农林牧渔/AI/新能源/半导体/医药）|
| **基本面** | 25% | 业绩（PE 间接）、市值合理性、60日趋势稳定性 |

---

## 🛡️ 风险过滤

| 规则 | 说明 |
|---|---|
| ST / *ST 一律排除 | 名称含 ST 关键词 |
| 主板限定 | 沪 60 / 深 000、001、002 |
| 高位派发段 | 5日 ≥ 30% 或 10日 ≥ 50% → 不进 TOP |
| 换手过热 | 换手 ≥ 30% → 不进 TOP |
| 主力大幅净流出 | 净占比 < -5% → 不进 TOP |

---

## 🔧 常见问题

### Q1：推送没有准时到达？
GitHub Actions 的 cron 有时延迟 10-30 分钟，属于平台特性。如需精确到分钟，需升级到 GitHub Pro。

### Q2：钉钉群里没收到推送？
1. **检查 Secret 是否填对**：`DINGTALK_WEBHOOK` 必须以 `https://oapi.dingtalk.com/robot/send?access_token=` 开头
2. **加签模式**：检查 `DINGTALK_SECRET` 是否填了（必须以 `SEC` 开头）
3. **自定义关键词模式**：推送内容里必须出现「主升浪」三个字（默认已包含）
4. 看 GitHub Actions 日志，是否有 `推送失败` 字样 + 错误详情

### Q3：钉钉返回 "keywords not in content" 错误？
您选择了「自定义关键词」模式但没填「主升浪」。两种解法：
- 群机器人设置里把关键词改为「主升浪」
- 改用「加签」模式（更安全）

### Q4：钉钉返回 "sign not match" 错误？
您开启了加签但 `DINGTALK_SECRET` 没填或填错。检查 Secret 值是否以 `SEC` 开头且完整。

### Q5：如何修改推送时间？
编辑 `.github/workflows/daily.yml`，调整 cron 表达式。例如改为早上 9:00 北京时间：
```yaml
- cron: '0 1 * * 1-5'  # UTC 01:00 = 北京时间 09:00
```

### Q6：如何修改主题关键词？
编辑 `src/selector.py` 中的 `HOT_THEMES` 字典，添加您关注的题材关键词。

### Q7：如何更换评分权重？
编辑 `src/config.py` 中的 `SCORE_WEIGHTS`，合计必须为 100。

### Q8：数据抓取失败？
akshare 接口偶尔会变更，可升级：`pip install --upgrade akshare`。如果仍失败，去 GitHub Actions 日志查看详细错误。

---

## 📜 许可证

仅供学习交流，不构成任何投资建议。

---

## 🙏 致谢

- 数据源：[akshare](https://github.com/akfamily/akshare) — 开源 A 股数据
- 调度：GitHub Actions
- 推送：钉钉群自定义 webhook 机器人

> ⚠️ **免责声明**：本项目基于公开数据自动生成结果，仅供参考，不构成任何投资建议。投资有风险，决策需谨慎。