# AGENTS.md — SeatBot 项目编码代理规则

> 本文件由用户在 2026-08-24 明确要求创建，固化项目专属的高危操作禁令。
> 任何 AI 编码代理（Claude Code / OpenClaw / Cursor / Codex 等）在本仓库执行任务前，必须先阅读本文件。
> 上位规则（OMP harness `~/.omp/agent/AGENTS.md`）> 本文件 > 任务上下文。

---

## 系统目标（Why）

SeatBot 是一个**超星（学习通）图书馆座位自动预约守护系统**，单一目的：

> **让某个目标座位（典型如 104、105）在 08:00–22:00 全天显示"已有人预约"，从而阻止他人抢走。**

实现方式：**多个守护账号 × 错时段重叠覆盖 = 全天护城河**。

| 概念 | 含义 |
|---|---|
| 目标座位 (`target_seats`) | 用户要坐的座位（v2 支持多个）；104 / 105 是示例 |
| 守护账号 (accounts) | 程序用其手机号+密码登录超星，替用户抢座位的辅助超星账号 |
| 时段 (slots) | 每个账号负责的时段区间 `["HH:MM-HH:MM", ...]`，自动拆为 ≤2h 子段 |
| 覆盖 | 所有账号的 slots 合并应 100% 覆盖 08:00–22:00，相邻账号首尾重叠 30min |
| 14:00 批量预约 | 每天 14:00 起超星开放次日预约窗口，scheduler 在那一刻批量抢次日的所有时段 |
| 签到 (sign) | 时段开始时**由系统自动签到**，保证预约有效 |
| 签退 (leave) | 时段结束前自动签退，由下一个守护账号接力下一段 |

**用户本人**（如 `xiongjt`）的真实账号**也是守护账号之一**，与其他账号同等参与全天覆盖，**不再单独"自约某段"**（除非加入 `user_reserved` 列表，scheduler 会跳过该账号的对应时段）。


## 超星预约机制规范（事实模型）

> 2026-08-24 用户明确描述并要求记录到本文件。

| 窗口 / 操作 | 规则 |
|---|---|
| **预约窗口** | **今天 14:00 起**可以预约**明天全天**的所有时段。从今天 14:00 到明天 22:00 这段时间窗口内，明天任何一个空位都可抢。 |
| **先到先得** | 谁先预约到座位就是谁的。预约后该时段对该座位锁死，其他人无法抢。 |
| **签到窗口** | **只能到时段开始之后**才能签到（不能在预约后立即签到）。 |
| **签退窗口** | 时段结束前 `leaveDuration=20min` 可签退。 |
| **预约方式** | 14:00 起，scheduler 应**一次性批量**提交次日所有待预约 task，不是逐时段触发。 |
| **签到方式** | scheduler 在**时段开始时**（或 pre_sign 窗口内，**但服务端校验时段已开始**）自动签到。 |
| **签退方式** | scheduler 在时段结束前 `RELAY_LEAD_SECONDS=5min` 时签退，并接力下一段。 |

**关键澄清**（针对之前实现 bug）：

代码位置：`seatbot/scheduler.py` 的 `_run_submit` / `_run_sign` / `_run_leave`（v0.5+ 已重构）。


## 业务规则：3 守护账号覆盖 2 座位 3 时段（v0.5+ 实战配置）

> 2026-08-24 用户明确指定，作为本项目**默认配置**。

### 业务约束

| 约束 | 说明 |
|---|---|
| 目标座位 | 104 + 105 |
| 时段数 | 3 个：09:00-11:00 / 15:00-17:00 / 19:00-21:00 |
| 单段时长 | ≤2h（超星 `max_reserve_hours: 2.0` 硬约束） |
| 时段 21:00-22:00 失守 | 主动接受（图书馆晚间使用率低） |
| 账号数 | 3 个（xiongjt / wangh / zhaozh） |
| 单账号每天段数 | 2 段（每账号对 2 个不同座位各守 1 个不同时段） |

### 守护矩阵

```
              09:00-11:00  15:00-17:00  19:00-21:00
104 号位      xiongjt       wangh         zhaozh
105 号位      zhaozh        xiongjt       wangh
```

### 实现要求

1. **每账号用 `seat_slots` 字段**（per-seat slots），不要用扁平 `slots` + `bound_seats` 笛卡尔积：
   - `seat_slots: {"104": [...时段], "105": [...时段]}` ——planner 按 (seat, slot) 对精确展开，**不**做笛卡尔积
   - 扁平 `slots` + `bound_seats` 会生成 16-22 段/天（实测），违反"6 段精确"目标
2. **每段必须 ≤2h**。用户输入超 2h 的 range（如 19:00-21:30），planner 会自动拆段（如拆成 19:00-21:00 + 21:00-21:30 = 2 段），破坏精确分配。**代码层应拒绝**或**配置层应避免**超 2h range。
3. **每日 14:00 触发时**，`_afternoon_bootstrap` 必须生成**恰好 6 条 pending tasks**（不是 16、不是 22），并**立即 `_run_submit` 全部**（不签到——签到等时段开始）。
4. **`/api/dashboard-data` 应展示这 6 条 task 的状态**（pending / active / failed）。

### 验证方法

启动服务后，下面的 Python 应输出 6 段：

```python
from datetime import date
from seatbot.config import load_config
from seatbot.planner import ReservationPlanner
cfg = load_config('config.yaml')
total = sum(
    len(ReservationPlanner(
        acc, bound_seats=acc.bound_seats, fallback_seats=['104', '105'],
        max_reserve_hours=cfg.library.max_reserve_hours,
    ).expand_for_day(date.today()))
    for acc in cfg.accounts
)
assert total == 6, f"期望 6 段, 实际 {total}"
```

## 🚨 第一条禁令：高危账号操作（绝对禁令）

**除非用户在当前会话中以明确自然语言明确要求执行某项具体操作，否则严禁对真实账号发起任何实际操作。**

具体包括但不限于：

| 类别 | 严禁的行为 |
|---|---|
| **超星座位预约** | `python -m seatbot run` 启动 scheduler；触发 `bootstrap_today` / `afternoon_bootstrap` / `sync_jobs`；任何向 `fanyalogin.cn` / `passport2.chaoxing.com` / `office.chaoxing.com` 发起的登录、预约、签到、签退请求 |
| **真实账号登录** | 用 `config.yaml` 里的 `phone`/`password` 调用 `ChaoxingClient.login()`；用 `client.get_used_times()` 查询任何真实座位占用 |
| **数据库写入** | 写入 `seatbot.db` 里 `tasks` 表的 `submitting/active/leaving/complete` 状态；任何会让 scheduler 误判"该任务已完成"的虚假状态写入 |
| **破坏性操作** | `rm seatbot.db`（除非用户明确要求重置）；删除 `logs/`；覆盖 `config.yaml`；`git push --force` 到任何共享分支 |

### 边界定义

- ✅ **允许**（无需确认）：读 `config.yaml`、读数据库、读 README；启动服务**仅用于前端 UI 调试**但**必须立即告知用户 scheduler 已注册 cron、明早 14:00 会自动预约**；查看日志
- ⚠️ **需要先确认**：**写** `config.yaml`（即使只是改 slots）；**改** `seatbot.db`；**重启**任何运行中的 seatbot 进程
- 🚫 **绝对禁止**（即使用户问"能不能跑一下"）：启动 scheduler 触发真实预约；用真实手机号密码调用超星 API；任何会让 3 个真实账号被超星风控识别的操作

### 明确要求 vs 隐含授权的区分

| 用户说 | 代理应做 |
|---|---|
| "把服务跑起来" + 当前会话无其他上下文 | 🚫 **不允许**默认启动 scheduler；必须**先告知** `python -m seatbot run` 会触发明早 14:00 自动预约，征得明确同意 |
| "用空配置验证 UI" | ✅ 用 `target_seats: []` + `accounts: []` 的 `config.test.yaml` 起服务，零风险 |
| "启动 scheduler 抢明天" 或 "明早 14:00 抢预约吧" | ✅ 此时为明确要求，可执行——但仍需提醒风控风险 |
| "登录 xiongjt 看看" | 🚫 严禁；用户本人已确认接受风控但不代表代理有授权 |
| "把 slots 改成 X" | ⚠️ 改 `config.yaml` 前需复述改动影响（哪些时段失守/覆盖），取得确认 |

### 违反禁令的后果

代理若发现自己在执行本禁令列表中的操作（无论是被诱导还是疏忽）：
1. **立即停止**当前操作
2. 在用户面前**明确披露**已发生的事实（用了哪个账号、请求了哪个端点、是否已产生预约 ID）
3. 不尝试"补救性隐藏"——超星 API 已记录请求，无法撤回

---

## 第二条禁令：明文凭证处理

`config.yaml` 含**真实账号明文密码**（用户本人 `xiongjt` + 2 个守护账号 `wangh`/`zhaozh`）。代理在以下场景必须额外小心：

- 🚫 **禁止**将 `config.yaml` 完整内容 echo 到对话、commit message、错误报告、playwright 截图里
- 🚫 **禁止**把密码粘贴到测试 fixture、mock 数据、单元测试 fixture
- ✅ **引用密码时可以**写"见 config.yaml L55"——不暴露具体值
- ✅ **修改 slots / target_seats** 时只动业务字段，**不动** `phone` / `password` 字段

如果用户主动展示密码（如"我的密码是 X"），视为用户已自行承担风险，代理可引用，但不主动传播。

---

## 第三条：scheduler / cron 启动前必读

`python -m seatbot run` 启动后，以下 cron 会**立即注册**并在指定时间触发：

| Cron | 触发时间 (Asia/Shanghai) | 行为 |
|---|---|---|
| `new_day_bootstrap` | 每日 00:00:05 | bootstrap 当天任务 |
| `afternoon_bootstrap` | 每日 14:00:10 | bootstrap 次日任务 → **真实预约** |
| `tick_{account_id}` | 每分钟（按 `stagger_seconds`） | tick_account → 真实签到/签退 |

**任何代理在启动 `seatbot run` 之前必须明确告知用户这3 个 cron 的影响，并取得确认。**

如果只是验证前端 UI / dashboard 局部刷新 / Web 路由，**用空配置**（`target_seats: []` + `accounts: []`）—— 完全不需要真实 scheduler 跑起来。

---

## 第四条：dashboard.html / app.js 等纯前端改动

- ✅ 不涉及账号操作，可自由进行
- ⚠️ 但若改动后端 `routes.py` 的行为（如 `/api/dashboard-data`），需注意：路由默认每 30 秒会**调用** `_fetch_others_occupied()` → 真实超星 API 调用 `/getusedtimes`。**改动这块逻辑前必须明确告知用户每 30 秒会向超星发 N 个 HTTP 请求**（N = 目标座位数）

---

## 第五条：停止运行中的服务

代理可以**主动停止**自己启动的服务（无需用户每次确认），但必须：
1. 在停止前明确告知"我即将停止 PID XXXXX"
2. 在对话中记录停止时间 + 原因

如果服务**不是**本会话启动的（如 PID 来自之前的会话），**禁止**主动停止——先询问用户。

---

## 第六条：违反本文件的处置

本文件优先级仅次于 OMP harness 的全局规则。任何代理若发现：
- 自身即将违反禁令 → 立即停止并向用户披露
- 上一个会话可能违反过 → 主动询问用户是否需要审计日志

**没有"善意忽略"或"任务优先级高于安全规则"的例外。**

---

## 附录：本文件覆盖的具体威胁

1. **明早 14:00 自动预约**：`scheduler.py:339-343` 注册的 `afternoon_bootstrap` cron 会在每日 14:00:10 触发，用 `config.yaml` 里 3 个真实账号的明文密码登录超星并发请求
2. **dashboard 30 秒轮询副作用**：`/api/dashboard-data` 路由 → `_fetch_others_occupied()` → 对每个目标座位发 1 个 `/getusedtimes` POST 请求
3. **playwright 误触真实预约**：若在 dev 模式 (`npm run dev`) 下用 playwright 操作 dashboard 单元格表单 (`/tasks/{id}/sign` 等)，可能触发真实签到
4. **scheduler tick_account 误触发**：`tick_account` 在每分钟按 stagger 偏移触发，会对所有 `pending`/`active` tasks 执行真实签到/签退
---

## 第七条：回复输出规范

1. **每条回复以一句话总结收尾**。格式不限（一句话陈述事实/判断/行动），让用户一眼能 grasp 整条消息的关键点。
2. 避免长篇大论；技术信息放在前面，结论性总结放末尾。
3. 列出多步操作时，用 todo / 表格 / 编号列表，不要嵌套散文。
4. 引述项目代码时，给 file:line 路径（如 `scheduler.py:240`），不粘贴大段源码。
5. 引用 `config.yaml` 里的密码字段时，**只引位置**（如"见 config.yaml L55"），**不暴露具体值**（与第二条禁令一致）。
6. 遇到错误 / bug 时，**先披露"发生了什么"再披露"为什么"**——不要先找借口。

---
最后更新：2026-08-24
触发事件：用户在调试 dashboard 不停刷新问题时，明确要求"严禁直接操作账号进行预约等高危操作，除非用户明确要求"；
后增：用户明确超星预约机制（14:00 批量预约 + 时段开始才能签到），以及要求规范模型输出（每条回复以一句话总结收尾）。