# Frontend Redesign — Design Spec

- **Date**: 2026-07-08
- **Author**: brainstorming session (user-approved direction; skipped section review per user request)
- **Target**: SeatBot Web 面板（FastAPI + Jinja2）前端全面重构
- **Backend constraint**: 不动业务逻辑，仅追加 `GET /api/status`
- **Stack constraint**: Jinja2 SSR，零 JS 框架，零 CSS 框架，零构建步骤

## 0. 一句话

把现有 7 个 Web 页面从手写零散 CSS 重构为统一设计系统：**扁平企业 SaaS 视觉 + 暗/亮双主题 + 左导航 + 全部 7 页重构 + 桌面与移动端适配 + 新增人体时钟小甜点 + 视觉回归截图脚本**。

## 1. Goals

1. 单一 `style.css`（设计 token + 全局样式）+ 多文件 `templates/_macros/*.html`（sidebar / topbar / card / table / badge / banner / form / gantt / popover）
2. **左导航**：220px 固定深色侧栏（桌面端）；移动端折叠为顶部抽屉
3. **7 个页面全部重构**：dashboard / seat-config / accounts-list / accounts-form / coverage / tasks / seats / logs
4. **暗色主题默认**（内网 7×24 盯屏场景），[亮/暗] 切换按钮放侧栏底部；选择写入 `localStorage`
5. **新增 `GET /api/status`**：返回 `{now, next_relay_in_min, next_relay_account_id, last_log}`，给顶部人体时钟用
6. **Dashboard 单元格 hover 出操作菜单**：[立即签到] [立即签退] [续约下段] [取消]（原生 `<details>` popover，零依赖）
7. **空状态插画 + 友好提示**：每个列表页（账号/任务/日志/覆盖）无数据时显示
8. **响应式**：≥ 1280px 完整布局；1024–1279px 侧栏收窄到 64px 仅图标；< 1024px 侧栏改顶部抽屉
9. **保留全页 30s reload**（沿用现状，不引入新依赖）
10. **视觉回归截图**：`scripts/snap.py` 抓 7 个页面到 `docs/screenshots/{desktop,mobile}/` 供对照

## 2. Non-Goals

- 不引入 JS 框架（Vue / React / HTMX / Alpine）
- 不引入 CSS 框架（Tailwind / Bootstrap / DaisyUI）
- 不动后端业务逻辑（scheduler / client / store / coverage / enc / planner）
- 不重构 `routes.py` 的 URL 与表单处理（保留全部向后兼容的接口）
- 不改现有 API 行为（`/api/seats/{room_id}`、`/api/coverage`、`/api/seats/availability` 都不动；仅**新增** `/api/status`）
- 不做用户系统 / 鉴权（沿用"内网 + 无 auth"）
- 不做 v2 通知 / 多馆切换 / 任务模板（保留在 v2 backlog）
- 不做生产构建优化（不引入 terser / purgeCSS / Vite）

## 3. Design Tokens

CSS 自定义属性集中在 `style.css` 顶部 `:root` 与 `:root[data-theme="light"]`。

### 3.1 色板（暗色默认 / 亮色切换）

```
--bg-app:        #0b1220  (dark) / #fafafa (light)
--bg-elevated:   #0f172a  (dark) / #ffffff (light)
--bg-surface:    #131c2f  (dark) / #ffffff (light)
--bg-muted:      #1e293b  (dark) / #f1f5f9 (light)
--border-subtle: #1e293b  (dark) / #e2e8f0 (light)
--border-strong: #334155  (dark) / #cbd5e1 (light)

--text-primary:  #f1f5f9  (dark) / #0f172a (light)
--text-secondary:#94a3b8  (dark) / #475569 (light)
--text-muted:    #64748b  (dark) / #64748b (light)
--text-inverse:  #0f172a  (dark) / #ffffff (light)

--accent:        #818cf8  (dark) / #4f46e5 (light)   # 靛青，导航高亮 / 强调
--accent-hover:  #a5b4fc  (dark) / #6366f1 (light)
--accent-soft:   #1e1b4b  (dark) / #eef2ff (light)   # 当前页导航背景

--success:       #10b981  (dark) / #059669 (light)
--warn:          #f59e0b  (dark) / #d97706 (light)
--danger:        #ef4444  (dark) / #dc2626 (light)
--info:          #38bdf8  (dark) / #0284c7 (light)
```

**状态色收敛**（与现状的 5+ 状态对比）：把 `active/submitting/leaving/pending/failed` 收敛为 **3 类语义色**：

| 现状枚举 | 新映射 |
|---|---|
| `active` | `--success` |
| `submitting` | `--info` |
| `leaving` | `--warn` |
| `pending` | `--bg-muted`（仅用 cell 边框或下划线表达，不用填充色） |
| `failed` | `--danger` |
| `complete` | `--success` 但透明度 0.4（已结束，淡化） |

### 3.2 排版

```
--font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif
--font-mono: ui-monospace, SFMono-Regular, "JetBrains Mono", Menlo, monospace

--fs-xs:  11px
--fs-sm:  12px
--fs-base:13px
--fs-md:  14px
--fs-lg:  16px
--fs-xl:  20px
--fs-2xl: 24px

--lh-tight: 1.3
--lh-base:  1.55

数字类元素（时间、计数、座位号）一律：
  font-variant-numeric: tabular-nums;
  font-feature-settings: "tnum";
```

### 3.3 间距 / 圆角 / 阴影

```
--sp-1: 4px
--sp-2: 8px
--sp-3: 12px
--sp-4: 16px
--sp-5: 20px
--sp-6: 24px
--sp-8: 32px

--radius-sm: 4px
--radius-md: 6px       (默认卡片)
--radius-lg: 8px
--radius-pill: 999px

--shadow-sm:  0 1px 2px rgba(0,0,0,.06)        (light 主题才使用)
--shadow-md:  0 2px 6px rgba(0,0,0,.08)
暗色主题不用 box-shadow；改用 border + 微妙 gradient
```

### 3.4 布局常量

```
--sidebar-w:        220px    (≥ 1280px)
--sidebar-w-collapsed: 64px   (1024–1279px, 仅图标)
--sidebar-h-mobile: 56px     (< 1024px, 顶部抽屉)
--content-max-w:    1440px
--content-padding:  24px
```

## 4. Layout

### 4.1 桌面 ≥ 1280px

```
┌─────────────┬────────────────────────────────────────────┐
│             │  Topbar (h: 56px)                         │
│  Sidebar    │  ── 页面标题 + 人体时钟                    │
│  220px      ├────────────────────────────────────────────┤
│             │                                            │
│  Logo       │  Content                                   │
│  Nav        │  (max-w: 1440px, padding 24px)             │
│  items      │                                            │
│             │                                            │
│  主题切换   │                                            │
│  v0.4.2     │                                            │
└─────────────┴────────────────────────────────────────────┘
```

### 4.2 1024–1279px：侧栏收窄 64px，仅图标 + Tooltip

### 4.3 < 1024px：侧栏变顶部抽屉（汉堡菜单）；Gantt 横向滚动；表格转卡片列表（账号/任务/日志）

## 5. Component Library（Jinja2 Macros）

### 5.1 `sidebar.html`
- 参数：`active`（当前页 key）
- 输出：完整侧栏 nav，7 项，每项右侧可放 count badge
- 当前页：背景 `--accent-soft`，文字 `--accent`
- 底部固定：主题切换按钮（☀ / 🌙 双字符）、版本号

### 5.2 `topbar.html`
- 参数：`title`（页面 H1）、`subtitle`（可选）
- 内部用 JS 每 30s fetch `/api/status` 刷新右侧人体时钟
- 显示：`现在 HH:MM · 下次接力 HH:MM by {account}`

### 5.3 `card.html`
- 参数：`title`（可选）、`body`（slot）、`footer`（可选 slot）
- 输出：白底/暗色 `--bg-elevated`，1px 边框 `--border-subtle`，圆角 6px

### 5.4 `stat_card.html`
- 参数：`label`、`value`、`hint`（可选）、`accent`（可选布尔）
- 输出：1px 边线分隔的统计卡（dashboard 用 4 张并排）

### 5.5 `table.html`
- 参数：`headers`、`rows`、`empty_title`、`empty_hint`、`actions`（可选列）
- 行 hover 高亮（`--bg-muted`）
- 表头：`text-transform: uppercase; letter-spacing: 0.5px; color: --text-secondary; font-size: 11px`
- 空状态：内嵌 SVG 插画 + 引导文字 + CTA 按钮

### 5.6 `badge.html`
- 参数：`status`（active/submitting/leaving/pending/failed/complete）
- 输出：6px 圆点 + 文字，圆角 pill 形

### 5.7 `banner.html`
- 参数：`level`（error/warn/info/ok）、`text`
- 输出：左 2px 边线 + 6px 圆点 + 文字，无填充色背景

### 5.8 `form.html`
- 字段宏：`<input>`、`<select>`、`<textarea>`，统一 label 在上方、helper text 在下方
- 按钮组：主按钮 `--accent`，次按钮 `--bg-muted + --text-primary`

### 5.9 `gantt.html`
- 参数：`accounts`、`cells`、`gaps`、`overlaps`
- 输出 Gantt 表格 + 单元格 popover（`<details>` 原生）
- 单元格 popover 内含：[立即签到][立即签退][续约下段][取消] 四个表单按钮
- 仅 `cell-active` / `cell-failed` / `cell-submitting` 状态出 popover（`cell-pending` / `cell-empty` 不出，避免噪声）

### 5.10 `popover.html`
- 用原生 `<details><summary>` 实现，无需 JS
- 移动端：tap 切换；桌面端：hover + focus 都展开（CSS `:hover :focus-within`）

## 6. Pages（7 个全部重构）

| 路由 | 页面 | 用到的 macros |
|---|---|---|
| `GET /` | dashboard | sidebar, topbar, stat_card×4, banner, gantt |
| `GET /seat-config` | seat-config | sidebar, topbar, card, form, banner |
| `GET /accounts` | accounts-list | sidebar, topbar, table |
| `GET /accounts/new` `/accounts/{id}/edit` | accounts-form | sidebar, topbar, card, form, banner |
| `GET /coverage` | coverage | sidebar, topbar, banner, gantt |
| `GET /tasks` | tasks-list | sidebar, topbar, table, badge |
| `GET /seats` | seats | sidebar, topbar, card |
| `GET /logs` | logs | sidebar, topbar, table |

每个页面在 `<html data-theme="...">` 上携带主题，CSS 通过 `data-theme` 选择器切换。

## 7. Backend Changes

### 7.1 新增 `GET /api/status`

```python
@router.get("/api/status")
async def api_status(request: Request):
    sched = request.app.state.sched
    store = request.app.state.store
    now = now_cst()
    # 取下一次接力
    next_relay = await sched.peek_next_relay()  # 新增方法
    # 取最近一条日志
    last_logs = await store.list_logs(limit=1)
    return JSONResponse({
        "now": now.isoformat(timespec="seconds"),
        "next_relay_in_min": (next_relay.delta_minutes if next_relay else None),
        "next_relay_at": (next_relay.at.isoformat(timespec="minutes") if next_relay else None),
        "next_relay_account_id": (next_relay.account_id if next_relay else None),
        "last_log": (last_logs[0].__dict__ if last_logs else None),
    })
```

`Scheduler.peek_next_relay()` 从已 bootstrap 的 tasks 表里查 `start_time + (end_time - 5min)` 之后且 `status=pending` 最早一条 task。

### 7.2 不变的接口
全部现有 GET / POST 路由保留。仅追加。

## 8. JavaScript（仅 2 个极小文件）

### 8.1 `theme.js` (~30 行)
- 读 `localStorage.theme`，默认 `dark`
- 切 `<html data-theme="...">`
- 切完写回 `localStorage`
- 监听系统 `prefers-color-scheme` 变化（仅在用户未手动切过时同步）

### 8.2 `clock.js` (~40 行)
- 每 30s `fetch('/api/status')`
- 更新 topbar 的 `#now-time` / `#next-relay` / `#next-relay-account` 三个 DOM 节点
- 网络失败：保留上次值，不报错

**不用任何库**。`fetch + DOMContentLoaded + setInterval`。

## 9. File Layout（变更）

```
seatbot/web/
├── app.py                 不动
├── routes.py              追加 1 个 /api/status + peek_next_relay stub
├── templates/
│   ├── base.html          改：left-sidebar 布局
│   ├── dashboard.html     改：macros
│   ├── seat_config.html   改：macros
│   ├── accounts_list.html 改：macros + empty state
│   ├── accounts_form.html 改：macros
│   ├── coverage.html      改：macros
│   ├── tasks_list.html    改：macros + empty state
│   ├── seats.html         改：macros
│   ├── logs.html          改：macros + empty state
│   └── _macros/           新建
│       ├── sidebar.html
│       ├── topbar.html
│       ├── card.html
│       ├── stat_card.html
│       ├── table.html
│       ├── badge.html
│       ├── banner.html
│       ├── form.html
│       ├── gantt.html
│       └── popover.html
├── static/
│   ├── style.css          重写
│   ├── theme.js           新建
│   └── clock.js           新建

scripts/
├── snap.py                新建：playwright 抓 7 页 × {desktop, mobile} = 14 张
docs/screenshots/
└── {desktop,mobile}/      snap.py 输出
```

## 10. Error Handling

| 错误 | 行为 |
|---|---|
| `/api/status` 失败 | clock.js 静默，保留 last value |
| `peek_next_relay()` 无下次接力（22:00-08:00） | 返回 `null`，UI 显示 "夜间静默 · 下次 bootstrap HH:00:05" |
| 主题切换时无 `localStorage`（隐私模式） | 仅本次会话有效 |
| 移动端抽屉点空白处 | 原生 `<details>` toggle 自动收起 |

## 11. Testing

- 单元测试：`tests/test_web_status.py` 测试 `/api/status` 返回 schema
- 视觉回归：`scripts/snap.py` 抓 7 页 × 2 视口 = 14 张图，人眼对比 before/after
- 手动验收清单（在 plan 里展开）：左导航 / 暗色主题 / 单元格 popover / 移动端抽屉 / 空状态 / 人体时钟刷新

## 12. Deployment

- 零新增依赖
- `python -m seatbot run` 启动方式不变
- 部署/Dockerfile/systemd 不动
- `.gitignore` 增加 `docs/screenshots/`（自动产物不入仓）

## 13. Open Questions / Future

- v2：把 `peek_next_relay()` 改成更精确的"下一个 pending task 的 (start - 5min)"
- v2：暗色主题做一套 Linear 风格的彩色 accent
- v2：移动端把 Gantt 转日历视图
- v3：登录 + 多用户隔离