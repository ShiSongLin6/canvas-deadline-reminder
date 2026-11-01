# Canvas Deadline Reminder

> 把 Canvas 的作业截止时间推送到微信，截止前提醒一次、当天中午汇总一次。

Canvas 自带的通知不太靠谱——邮件容易被埋、App 推送经常延迟，而作业截止时间是那种"错过就没了"的东西。这个脚本就是补这个缺口：**在你真正会看到的地方（微信）提醒你，而且只在必要的时候提醒。**

## 特性

- **截止前约 2 小时推一次**，每份作业只推一次，已提交的不推
- **当天中午推一次汇总**，列出今天所有待交作业；当天没作业就完全不打扰
- **不重复轰炸**：推送记录落在 `state.json`，重启、重跑都不会重复发
- **只看未来 7 天**，可调
- **时区自动跟随夏令时**（`Europe/London` 等 IANA 时区），不用手动改
- **令牌到期提醒**：Canvas 强制令牌 30 天过期，到期前 5 天开始每天提醒你续期
- 纯标准库 + `requests`，无框架、无数据库、无服务端

## 它长什么样

微信通知栏里看到的是通知标题（不是正文），所以标题被设计成"独立可读"：

| 场景 | 通知标题 |
| --- | --- |
| 截止前提醒 | `CV Week 5 11-06 23:59截止` |
| 中午汇总 | `今天2项待交 最晚21:00` |
| 令牌续期 | `Canvas令牌还剩5天过期` |

点开「查看详情」才是完整信息：课程全名、作业全名、截止时刻、提交链接、已交/待交状态。

## 快速开始

### 1. 拿 Canvas Token

1. 打开 `https://<你的学校>.instructure.com/profile/settings`（或学校的 Canvas 域名）
2. 拉到页面最下方 **Approved Integrations** → **+ New Access Token**
3. Purpose 随便填，例如 `deadline reminder`
4. **Expiration 现在是必填项，最长 30 天** —— 选最远那天，点 Generate
5. 复制那串 token（**只显示这一次**）

> 这个 token 等同于你账号的读取权限。不要提交到仓库，不要发给别人。

### 2. 拿 Server酱 SendKey

1. 打开 <https://sct.ftqq.com/>，微信扫码登录
2. 进 **SendKey** 页面，复制 `SCT` 开头的那串
3. **同一页面扫码关注它的服务号** —— 这步不做的话，消息发得出去但你微信里收不到

Server酱 免费额度足够个人使用（每天 5 条起，具体以官网为准）。

### 3. 配置并运行

```bash
pip install -r requirements.txt
cp config.example.json config.json
# 编辑 config.json，填入 canvas_base / canvas_token / sct_sendkey
python canvas_reminder.py --dry-run    # 先看会推什么，不真发
python canvas_reminder.py --test       # 发一条测试消息到微信
python canvas_reminder.py              # 正式运行
```

## 配置项

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `canvas_base` | — | 你的 Canvas 地址，如 `https://canvas.example.edu` |
| `canvas_token` | — | Canvas Personal Access Token |
| `sct_sendkey` | — | Server酱 SendKey（`SCT` 开头） |
| `canvas_token_expires` | — | Token 到期日（`YYYY-MM-DD`），用于续期提醒；可留空 |
| `tz` | `Europe/London` | IANA 时区，自动处理夏令时 |
| `lead_minutes` | `120` | 目标提前量（分钟） |
| `window_low` / `window_high` | `95` / `165` | 实际触发窗口（剩余分钟数） |
| `noon_hour` | `12` | 当日几点之后发汇总 |
| `lookahead_days` | `7` | 只看未来多少天的作业 |
| `course_filter` | `[]` | 空 = 全部课程；填 `["CV","IIS"]` 只留课程名含这些词的 |
| `skip_locked` | `true` | 跳过已锁定的作业 |

### 为什么提前量是"约"2 小时

脚本**每小时被唤醒一次**，触发窗口必须比 60 分钟宽才不会整段漏掉，所以设成 95–165 分钟。实际提醒会落在**截止前 1 小时 35 分到 2 小时 45 分**之间，稳定早于 1.5 小时。

想更贴近整 2 小时，可以把调度间隔降到 30 分钟（cron 支持，Windows 计划任务需要建两个错开的任务）。

## 课程短码

通知标题空间有限，所以课程名会被压缩成短码。在脚本顶部的 `COURSE_CODES` 里配置：

```python
COURSE_CODES = [
    ("computer vision", "CV"),
    ("data intensive", "DIAI"),
]
```

键是课程全名里出现的片段（不区分大小写）。**留空则自动取课程名各词首字母**（最多 3 个）。

## 部署方式

脚本本身无平台依赖（`requests` + `zoneinfo`），三种跑法任选。

### 方式一：GitHub Actions（免费，不占本机）

仓库自带 `.github/workflows/reminder.yml`，每小时整点跑一次。

配置步骤：

1. 在仓库 **Settings → Secrets and variables → Actions** 里添加：
   - Secrets：`CANVAS_TOKEN`、`SCT_SENDKEY`
   - Variables：`CANVAS_BASE`、`CANVAS_TOKEN_EXPIRES`（可选）、`TZ_NAME`（可选）
2. 到 **Actions** 标签页启用 workflow
3. 用 **Run workflow** 手动跑一次验证

> 注意：GitHub 的 `schedule` 在高峰期可能延迟几分钟到十几分钟，对"提前 2 小时"的窗口影响不大。另外私有仓库若连续 60 天无提交活动，`schedule` 会被自动禁用，需要注意。

### 方式二：Linux 服务器 cron

```cron
0 * * * * cd /path/to/canvas-deadline-reminder && /usr/bin/python3 canvas_reminder.py >> logs/cron.log 2>&1
```

### 方式三：Windows 计划任务

在任务计划程序里建一个每小时触发的任务，**执行程序用 `pythonw.exe` 而不是 `python.exe`**。

这是最容易踩的坑：`python.exe` 是控制台程序，配合"仅在用户登录时运行"，会让黑色命令行窗口每小时闪一下。`pythonw.exe` 是无窗口版本，运行完全静默。

## 微信通知的坑（实测）

这一节可能是这个项目最有用的部分，都是实际踩出来的：

1. **通知栏只显示标题，不显示正文**。你看到的那一行是 `设备通知:` + 你的 title，正文要点「查看详情」才看得到。所以标题必须自己站得住。
2. **标题约 28 个字符后会被截断**，多出来的变成 `...`。别把关键信息放在末尾。
3. **emoji 会被渲染层吃掉**。写 `⏰ 2 小时后截止`，通知栏显示的是 `2 小时后截止`。标题里不要用 emoji。
4. **第一行大字「设备通知」改不了**。它是 Server酱 在微信模板库注册的模板标题，网页端没有切换入口。
5. **必须关注服务号**。不关注的话 API 返回成功、服务端也显示已送达，但你微信里什么都看不到。

## 常见问题

**返回 401**
Token 粘错了或已过期。重新生成一个，注意 Canvas 令牌最长只有 30 天。

**"活跃课程 0 门"**
token 所属账号不对，或者当前学期课程未在 Dashboard 显示（`enrollment_state=active` 之外）。去 Canvas 确认课程可见。

**某条提醒不想要了**
删掉 `state.json` 里对应的键。`pre:` 开头是截止前提醒，`day:` 开头是当天汇总。

**已提交但还在提醒**
判断依赖 Canvas 的提交记录。纸质提交、小组作业由他人代交这类情况 Canvas 没有记录，手动删键静音即可。

**电脑关机时会漏提醒**
本机部署必然如此。任务计划程序可以设"错过后补跑"，但开机时可能已越过触发窗口。要彻底解决就用 GitHub Actions 或云服务器。

## 隐私

- Token 和 SendKey 只存在本地 `config.json`，已列入 `.gitignore`，不会被提交
- 脚本只把作业标题和截止时间发给 Server酱，不发送成绩、文件、个人信息
- 用 GitHub Actions 部署时，密钥存放在 Repository secrets，不会出现在日志里

## License

[MIT](LICENSE)
