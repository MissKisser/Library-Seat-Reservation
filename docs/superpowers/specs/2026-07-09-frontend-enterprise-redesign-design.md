# 前端企业级重新设计 (Frontend Enterprise Redesign) — Design Spec

- **日期**: 2026-07-09
- **作者**: SeatBot
- **状态**: 设计中
- **前置**: `2026-07-08-frontend-redesign-design.md`（第一版重设计，本 spec 取代其实现层）、`2026-07-09-multi-seat-full-coverage-design.md`（v2 多座位产品规格）

## 0. 目标与非目标

### 目标
将 SeatBot Web 面板（8 个页面）重新设计，达到"大厂企业级"的质感与体验：一致的视觉系统、深/亮双主题并重、打磨到位的交互细节（尤其账号验证这类瞬时动作的 loading/反馈/批量操作）。

### 非目标（明确不做）
- **不改后端路由**：`routes.py` 的 URL、表单字段、API 端点结构全部保留。新增的交互（批量验证、内联状态、轮询）都在**现有端点之上**用 Alpine + fetch 实现，不新增 JSON 端点。**唯一允许的例外**：现有 303 重定向的 URL 上附加 query param（如 `/accounts?saved=1`）供前端 prgToast 显示反馈——这不改变路由结构、表单字段或 API，视为允许。
- **不引入 JS 前端框架**：不用 Vue/React。客户端交互用 Alpine.js（CDN，无构建依赖）。
- **不改产品逻辑**：调度器、预约流程、数据模型不动。
- **不持久化验证状态**：账号验证结果不写库（与"不改后端"一致），只做会话内/行内反馈。
- **不做用户认证/多租户**：仍是本地/内网单用户。

## 1. 已锁定的关键决策

| 维度 | 决定 | 来源 |
|---|---|---|
| 技术栈 | Jinja2 SSR（保留）+ Tailwind v3 + 构建 + Alpine.js（CDN） | 用户选择 |
| 部署 | 构建产物 `static/style.css` 入库，服务器零 Node 依赖 | 推荐，见 §2 |
| 页面范围 | 全部 8 页重做 | 用户选择 |
| 后端 | 不改 routes.py 路由/表单结构，不新增端点 | 用户选择 |
| 主题 | 深色默认 + 亮色，双主题并重（都是一等公民） | 用户选择 |
| 主色 | indigo `#818cf8`（深）/ `#4f46e5`（亮），克制中性 | 用户选择 |
| 调性 | Linear / Vercel / GitHub 后台风格 | 用户选择 |
| 落地节奏 | 方案 A：设计系统先行 + 逐页重做 | 用户认可 |
| 验证细节 | 按钮 loading + 行内结果、列表页行级验证 + 批量验证（不持久化） | 用户选择 |

## 2. 技术栈与构建

### 栈
```
Jinja2 SSR (保留)  →  渲染 HTML：base.html + 8 页 + _macros/
        +
Tailwind v3 + 构建   →  tailwind.config.js 定义 token
                        src/input.css (Tailwind 指令 + @layer components)
                        npm run build → static/style.css
        +
Alpine.js (CDN)     →  <script defer src="cdn"></script>
                        配套 static/app.js 封装通用行为
```

### 构建产物入库策略
`static/style.css` 是构建产物，**提交到 git**。服务器运行时只读这个文件，部署链路不变（仍是 `pip install + python -m seatbot run`）。

`package.json` / `tailwind.config.js` / `src/input.css` / `node_modules` / `.gitignore`（忽略 node_modules）是**开发依赖**，不进运行时。

开发流程：改 token/组件类 → `npm run build` → 重新生成 `static/style.css` → 提交产物。

### Tailwind 构建配置
- `tailwind.config.js`：`content` 扫描 `seatbot/web/templates/**/*.html`，`darkMode: 'class'`（由 `<html class="dark">` 控制），主题色扩展进 `theme.extend.colors`。
- `src/input.css`：
  ```css
  @tailwind base;
  @tailwind components;
  @tailwind utilities;
  @layer components {
    /* .btn .card .badge .input 等组件类 */
  }
  ```
- `package.json`：
  ```json
  {
    "scripts": { "build": "tailwindcss -i src/input.css -o seatbot/web/static/style.css --minify" },
    "devDependencies": { "tailwindcss": "^3.4.0" }
  }
  ```
- `.gitignore` 追加 `node_modules/`。

## 3. 设计系统 (Design System)

### 3.1 色彩 Token

双主题，深色为默认。主题通过 `<html class="dark">`（Tailwind `darkMode: 'class'`）+ CSS 变量双重驱动。

**中性面（surface）**
| Token | 深色 | 亮色 | 用途 |
|---|---|---|---|
| `--bg-app` | `#0b1220` | `#fafafa` | 页面底 |
| `--bg-elevated` | `#0f172a` | `#ffffff` | 顶栏/侧栏/弹层 |
| `--bg-surface` | `#131c2f` | `#ffffff` | 卡片 |
| `--bg-muted` | `#1e293b` | `#f1f5f9` | 表头/输入框底/hover |
| `--border-subtle` | `#1e293b` | `#e2e8f0` | 卡片/分割 |
| `--border-strong` | `#334155` | `#cbd5e1` | 输入框/聚焦前 |

**文字**
| Token | 深色 | 亮色 |
|---|---|---|
| `--text-primary` | `#f1f5f9` | `#0f172a` |
| `--text-secondary` | `#94a3b8` | `#475569` |
| `--text-muted` | `#64748b` | `#94a3b8` |
| `--text-inverse` | `#0f172a` | `#ffffff` |

**主色（indigo）**
| Token | 深色 | 亮色 |
|---|---|---|
| `--accent` | `#818cf8` | `#4f46e5` |
| `--accent-hover` | `#a5b4fc` | `#4338ca` |
| `--accent-soft` | `rgba(129,140,248,0.12)` | `#eef2ff` |
| `--accent-contrast` | `#ffffff` | `#ffffff` |

**语义色（双主题统一，带 soft 变体）**
| 语义 | 主色 | soft 背景 |
|---|---|---|
| success | `#10b981` | `rgba(16,185,129,0.12)` |
| warn | `#f59e0b` | `rgba(245,158,11,0.12)` |
| danger | `#ef4444` | `rgba(239,68,68,0.12)` |
| info | `#38bdf8` | `rgba(56,189,248,0.12)` |

Tailwind 里通过 `theme.extend.colors` 映射：`surface`, `surface-elevated`, `surface-muted`, `border-subtle`, `accent`, `success`, `warn`, `danger`, `info` 等，让模板里能写 `bg-surface text-secondary`。

### 3.2 状态色映射（5+ 任务状态 → 3 语义色）
沿用现有收敛逻辑，写入 badge 与 gantt cell：
| TaskStatus | 语义色 | badge 文案 | gantt cell |
|---|---|---|---|
| `active` | success | 已预约 | success 实心 |
| `submitting` | info | 提交中 | info 实心 |
| `leaving` | warn | 签退中 | warn 实心 |
| `pending` / `ready` | muted | 待执行 | muted 描边 |
| `failed` | danger | 失败 | danger 实心 |
| `complete` | success（0.4 不透明度） | 已完成 | success 半透 |
| `empty` | transparent | — | 空 |
| `gap` | danger 描边 | — | 失守 |
| `user_reserved` | info（蓝，0.75） | — | 👤 用户已预约 |
| `others_occupied` | muted 斜纹 | — | 🔒 他人占用 |

### 3.3 排版
- 字体栈：`--font-sans` = `-apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", "PingFang SC", system-ui, sans-serif`；`--font-mono` = `"JetBrains Mono", "SF Mono", Consolas, monospace`。
- 字号阶（比现有整体放大一档，更易读）：
  - `xs` 12px / `sm` 13px / `base` 14px（正文，原 13）/ `md` 15px / `lg` 18px / `xl` 22px / `2xl` 28px
- 行高：`tight` 1.25 / `base` 1.55。
- 数字一律 `font-variant-numeric: tabular-nums`（时间、座位号、计数）。

### 3.4 间距 / 圆角 / 阴影
- 间距阶：`4 / 8 / 12 / 16 / 20 / 24 / 32 / 40 / 48` px。
- 圆角：`sm` 6px / `md` 8px / `lg` 12px / `pill` 999px。
- 阴影：
  - 深色用"边框 + 极轻渐变"，不用重 box-shadow（保持现有调性）。
  - 亮色引入轻阴影：`--shadow-sm` `0 1px 2px rgba(0,0,0,0.05)`、`--shadow-md` `0 4px 12px rgba(0,0,0,0.08)`、`--shadow-lg` `0 8px 24px rgba(0,0,0,0.12)`。
  - 弹层（toast / 下拉 / popover）两主题都用 `--shadow-lg` 提供层次。

### 3.5 布局常量
- `--sidebar-w` 220px（≥1280px）
- `--sidebar-w-collapsed` 64px（1024–1279px，仅图标）
- `--sidebar-h-mobile` 56px（<1024px 顶部抽屉）
- `--topbar-h` 56px
- `--content-max-w` 1440px（居中）
- `--content-padding` 24px

## 4. 应用骨架 (App Shell)

### 4.1 整体结构
```
<body class="dark">  ← app.js themeInit() 控制（读 localStorage，默认深色）
  <div x-data="{ mobileNav: false }">  ← Alpine
    <aside class="sidebar">  ... </aside>           ← 固定左侧
    <div class="app-main">
      <header class="topbar" x-data="topbarClock()"> ... </header>
      <main class="app-content"> {% block content %} </main>
    </div>
    <div x-data="toast()" class="toast-container"> ... </div>  ← 右下角
  </div>
  <script src="alpine cdn" defer></script>
  <script src="/static/app.js" defer></script>
</body>
```

### 4.2 侧栏（Sidebar）
- 220px 宽，`--bg-elevated`，右侧 `--border-subtle` 分割。
- **品牌区**（56px）：📚 图标 + "SeatBot" 标题 + 副标题"座位守护"。底部一条 `--accent` 渐变线作为强调。
- **导航**（8 项），每项 `.nav-item`：
  - 左侧 SVG 图标（24px，stroke 风格，与 Linear 一致）。
  - 文字 label。
  - 右侧 `.count` 小药丸（目标座位数、守护账号数等，有数才显示）。
  - active 态：`--accent-soft` 背景 + `--accent` 文字 + 左侧 3px `--accent` 竖条。
  - hover：`--bg-muted` 背景。
  - 导航项与 `active_page` 映射：`dashboard`/`targets`/`accounts`/`user-reserved`/`coverage`/`tasks`/`seats`/`logs`。
- **底部区**：
  - 主题切换按钮（☀/🌙 图标 + "深色/浅色"文字），点击 `toggleTheme()` → 改 `<html>` class + 存 localStorage + Alpine 响应。
  - 版本号"v0.5.0 · 本地/内网"。

### 4.3 顶栏（Topbar）
- 56px 高，`--bg-elevated`，sticky，底部 `--border-subtle`。
- **左侧**：当前页标题 + 副标题（如"覆盖图 · 护城河实时状态"）。
- **右侧**：时钟组（Alpine `topbarClock()` 组件，30s 轮询 `/api/status`）：
  - 当前时间 HH:MM（tabular-nums，`--text-primary`）。
  - 分隔点。
  - "下次接力 HH:MM" + `by {account}`（`--text-secondary`）。
  - 轮询失败静默保留上次值（与现 clock.js 行为一致）。
- **移动端**（<1024px）：左侧变汉堡按钮 `@click="mobileNav = true"`，右侧只留当前时间。

### 4.4 内容区
- `max-w 1440px` 居中，padding 24px。
- 页面顶部统一 `.page-header`：H1 标题 + 副标题 + 右侧操作区（如"新建"按钮）。

## 5. 组件库 (Component Library)

所有组件类用 Tailwind `@layer components` 封装，在 `src/input.css` 定义；交互用 Alpine 属性驱动。

### 5.1 按钮 `.btn`
变体：`btn-primary`（accent 实心）、`btn-secondary`（muted 底 + border）、`btn-danger`（danger 实心）、`btn-ghost`（透明 + hover muted）、`btn-subtle`（accent-soft 底 + accent 文字）。
尺寸：默认（36px h，px-4）、`btn-sm`（28px）、`btn-icon`（正方形，仅图标）。
状态：`disabled:opacity-50 disabled:cursor-not-allowed`、`focus:ring-2 ring-accent`、`active:scale-[0.98]` 过渡。
**Alpine loading**：`x-data="{ loading:false }"` 配合 `:disabled="loading"` + `x-show="loading"` 的 spinner（SVG 旋转）。封装成可复用模式，见 `verify-button` §6。

### 5.2 卡片 `.card`
`bg-surface border border-border-subtle rounded-lg`。可选 `.card-header`（标题 + 右侧操作，底部 border）、`.card-body`（padding）、`.card-footer`（顶部 border，padding）。
hover 提升（仅可点击卡片）：`hover:border-border-strong transition`。

### 5.3 指标卡 `.stat-card`
`label`（xs，uppercase，secondary）+ `value`（2xl，primary，tabular-nums）+ 可选 `hint`（xs，muted）+ 可选 `accent` 变体（value 用 accent 色）。dashboard 用 4 列 grid。

### 5.4 表格 `.table`
- 表头：xs，uppercase，secondary，sticky top。
- 行：hover `--bg-muted`，行间 border-subtle。
- 数字列 `.num`（tabular-nums + 右对齐）。
- 操作列 `.actions`（右对齐，按钮组）。
- **响应式**（<1024px）：`.table-card` 变体，每行变卡片，`data-label` 属性作标签（沿用现有 `is-card-list` 思路）。
- 空状态：`.empty`（见 §5.8）。

### 5.5 表单原子 `.form-field`
- label 在上（xs，medium，secondary，必填加 `*` accent 色）。
- `.input` / `.select` / `.textarea`：`h-9`，`bg-muted`（深）/ `bg-white`（亮），border，`focus:border-accent focus:ring-2`。
- hint（xs，muted）/ error（xs，danger）在下。
- `.form-row`：2 列 grid（响应式叠 1 列）。
- 复选框组 `.checkbox-grid`：多列 grid，每项带 label。

### 5.6 Badge `.badge`
药丸形，`::before` 6px 圆点 + 文字。变体：`badge-success/warn/danger/info/muted/faded`。映射逻辑在 `_macros/badge.html`，输入 TaskStatus 输出 (variant, 文案)。

### 5.7 Banner `.banner`
左侧 3px 强调条 + dot + 文字。变体：`banner-info/warn/error/ok`。文字支持 `| safe`（嵌入 HTML 如计数）。

### 5.8 空状态 `.empty`
居中列：SVG 插图（120px，stroke 风格，随主题变色）+ 标题（lg，primary）+ 提示（sm，muted，max-w-sm）+ 可选 CTA 按钮。

### 5.9 Toast（全局，Alpine）
- 容器固定右下角，`fixed bottom-4 right-4 z-50`，垂直堆叠。
- 每个 toast：图标 + 标题 + 描述，`bg-elevated shadow-lg`，圆角，进入动画（slide-up + fade）。
- 变体：success（绿）/ error（红）/ info（蓝）/ warn（橙），左侧强调条 + 对应图标。
- `app.js` 暴露全局函数 `window.showToast({type, title, desc})`，Alpine 组件订阅。
- 自动消失 4s（error 持续到手动关闭）。

### 5.10 确认框（Alpine，取代原生 confirm()）
原生 `confirm()` 不符合企业级。封装一个 `confirm-modal` Alpine 组件：居中弹层 + 遮罩，标题 + 描述 + 取消/确认按钮，`window.confirmAction({title, desc, confirmText, danger})` 返回 Promise。用于所有删除/取消/签退操作。

### 5.11 Gantt 单元 + Popover（Alpine 驱动）
- cell：40px 宽 × 34px 高，状态色见 §3.2。`user_reserved` 显示 👤 角标，`others_occupied` 显示 🔒 + 灰斜纹。
- popover 改为 Alpine 驱动（替换现有 `<details>`）：`@click` 切换，`@click.outside` 关闭，浮层 `absolute` 定位，`shadow-lg`。内容按状态显示操作（立即签到/签退/续约/取消）。
- sticky 表头 + sticky 左列（座位标签）。

## 6. 验证交互详设计（重点）

这是用户特别强调的细节。分三层，全部基于现有 `POST /accounts/{id}/test-login` 端点，不新增后端。

### 6.1 `verify-button` 组件（编辑页 + 列表行复用）
封装成 Jinja2 macro + Alpine 组件，可复用：
```html
<!-- _macros 形式，渲染 -->
<button x-data="verifyButton('{{ account.id }}')"
        @click="run()"
        :disabled="running"
        class="btn btn-sm ...">
  <svg x-show="!running"> ... </svg>          <!-- 默认图标 -->
  <svg x-show="running" class="animate-spin"> ... </svg>  <!-- spinner -->
  <span x-text="label"></span>                 <!-- 文案随状态变 -->
</button>
<span x-show="result" x-text="result" :class="resultClass"></span>  <!-- 行内结果 -->
```
Alpine 组件 `verifyButton(id)`（在 app.js 定义）状态机：
- `idle`：按钮文案"验证登录"，默认图标。
- `running`：禁用，spinner 旋转，文案"验证中…"。
- `success`：文案变"已验证"，绿色对勾图标，行内显示"✓ 登录成功"。同时 `window.showToast({type:'success', title:'验证通过', desc: id})`。
- `failed`：文案"验证登录"，红色叉图标（短暂），行内显示可读失败原因（见下）。`showToast({type:'error', ...})`。

**可读失败原因映射**（app.js，把后端返回的 `{ok:false, error}` 翻译成中文）：
| 后端 error 关键词 | 显示 |
|---|---|
| `timeout` / `TimeoutError` | "登录超时，请重试" |
| `password` / `401` / `账号或密码` | "手机号或密码错误" |
| `风控` / `risk` / `vc3` / `auth cookies` | "登录被风控拦截，可能需要手动登录" |
| `network` / `Connection` | "网络连接失败" |
| 其他 | 原始 error（兜底） |

### 6.2 列表页行级验证
`accounts_list.html` 每行操作列增加"验证"按钮（用 `verify-button` macro）。点击后该行按钮变 loading，结果在该行内联显示。互不影响其他行。

### 6.3 批量验证（"全部验证"）
列表页顶部操作区加"全部验证"按钮，Alpine 组件 `verifyAll(ids)`：
- 顺序（非并行，避免同时多次 Playwright 登录压垮）遍历所有 account id，逐个调用 `verifyButton(id).run()`。
- 按钮自身变 loading，文案"正在验证 i/N…"。
- 顶部一个进度条/计数。
- 完成后 toast 汇总："验证完成：X 通过，Y 失败"。
- 失败的行保持红色失败标记，方便定位。

**不持久化**：刷新页面后状态重置（符合"不改后端"）。批量验证期间禁用单个验证按钮避免冲突。

## 7. 页面设计（8 页）

每页：用途 + 布局 + 关键交互。所有页面共享 shell（§4）、组件库（§5）。

### 7.1 `/` 覆盖图（Dashboard）
**布局**：
- 顶部 `.page-header`：标题"覆盖图" + 副标题"护城河实时状态" + 右侧"刷新"按钮（手动 + Alpine 30s 自动 `location.reload`，带倒计时提示"30s 后自动刷新"）。
- 4 列 `.stat-grid`：目标座位数、守护账号数、今日覆盖（covered/total，有 gap 时 accent 警示）、当前时间。
- `.banner`：状态总览（无目标/全覆盖/有失守/occ_err 四态）。
- 主区 `.dashboard-grid`（响应式）：
  - 左主卡：Gantt 覆盖图（按座位，y=座位，x=08:00–22:00），cell popover 操作。提示"点击单元格操作 · 30s 自动刷新"。
  - 右侧栏（320px，移动端折叠到下方）：
    - 目标座位列表卡（含"全部 →"链接）。
    - 守护账号列表卡（含"管理 →"链接）。
    - 最近活动 feed（8 条，彩色 level dot）。
    - 快速操作（4 个 tile：新增目标、添加账号、覆盖报告、座位图）。

### 7.2 `/targets` 目标座位
**布局**：
- `.page-header`：标题 + 右侧无（内联添加）。
- 两卡布局：
  - 卡 1"添加目标座位"：内联表单（seat_num + label + 提交按钮），吸收原 targets_form.html。
  - 卡 2"已注册目标座位"：表格（座位号、标签、绑定账号 chips、操作[删除]）。空状态用 `.empty`。
- 删除用 `confirm-modal`（§5.10），PRG 提交后 toast 反馈（通过 query param 或 Alpine 拦截）。

### 7.3 `/accounts` 守护账号列表
**布局**：
- `.page-header`：标题"守护账号" + 右侧"新建账号"按钮 + "全部验证"按钮（§6.3）。
- `.card`：表格列：ID（code）、手机号、时段、绑定座位（chips 或"通用 wildcard"）、每日上限、**验证状态**（行内，§6.2）、操作（编辑、删除、验证）。
- 空状态 `.empty` + CTA"添加第一个守护账号"。
- 删除用 `confirm-modal`。

### 7.4 `/accounts/new` & `/accounts/{id}/edit` 守护账号表单
**布局**：
- `.page-header`：标题（新建/编辑）+ 右侧"返回列表"。
- `.card` 表单：
  - ID（新建可编辑，pattern 限制；编辑禁用，hint"保存后不可修改"）。
  - 手机号（必填）。
  - 密码（password，必填）。
  - 时段模式 select（full / custom）——Alpine `x-data` 控制自定义 textarea 显隐（修复现有 id 不一致 bug）。
  - 自定义时段 textarea（JSON 数组）。
  - 绑定座位 checkbox-grid（空 = wildcard，hint 说明）。
  - 每日并发段数 number。
  - **编辑模式特有**：`verify-button`（§6.1）放在表单顶部或独立卡，验证结果行内显示。
- 操作区：保存（primary）、取消（ghost 链接）。
- 表单验证：HTML5 required + pattern，Alpine 增强提交前检查（如 custom 模式下 textarea 非空、JSON 合法），失败 toast。
- 保存走 PRG，成功后重定向列表 + toast（通过 flash 机制或 query param）。

### 7.5 `/user-reserved` 用户硬预约
**布局**：
- `.page-header`：标题 + 说明"调度器会跳过这些时段"。
- 卡 1"添加硬预约"：表单（account_id select、seat_num select、day date、start/end time、note、提交）。
- 卡 2"已登记硬预约"：表格（账号、座位、日期、时段、备注、操作[删除]）。
- 表单校验 end > start（Alpine + 后端已有校验），删除 confirm-modal。

### 7.6 `/coverage` 覆盖报告
**布局**：
- `.page-header`：标题 + 日期选择（可选 day 参数）。
- `.banner`：如有 gap，error banner 显示失守总数 + 座位数；否则 ok banner。
- info/warn banner：Chaoxing others-occupied 数据说明。
- 主卡：Gantt（按座位）。
- 失守明细：每座位一张 `.card`（danger 边框），列出 gap 时段 `HH:MM – HH:MM`。

### 7.7 `/tasks` 任务
**布局**：
- `.page-header`：标题 + 筛选表单（account_id select、seat_num select、day input、筛选/重置）。
- `.card` 表格（9 列：ID、账号、座位、日期、时段、状态 badge、reserve_id、错误、操作）。操作按状态条件显示（签到/签退/取消，用 `confirm-modal` 对取消/签退）。
- 筛选用 GET 表单（PRG 一致）。
- 空状态提示"运行 `python -m seatbot run` 生成任务"。

### 7.8 `/seats` 座位图
**布局**（保留客户端渲染，Alpine 化）：
- `.page-header`：标题 + 副标题"实时座位（30s 刷新）"。
- `.card`：图例（free/occupied/target/error）+ `#seats-sections`。
- Alpine 组件 `seatMap(roomId)`：30s 轮询 `/api/seats/{room_id}`，按目标座位分组渲染 ±2 网格，occupied/target/error 着色，tooltip 显示占用信息。替换现有 setInterval + innerHTML。
- 错误态：轮询失败显示 `.banner banner-warn`"获取座位失败"。

### 7.9 `/logs` 日志
**布局**：
- `.page-header`：标题 + 筛选表单（account_id select、level select、筛选/重置）。
- `.card` 表格（时间、级别 badge、账号、消息 mono）。最多 300 行。
- 筛选用 GET 表单。

## 8. 通用交互规范 (Alpine 行为清单)

`static/app.js` 封装以下 Alpine 组件/全局函数，全站复用：
| 名称 | 类型 | 作用 |
|---|---|---|
| `topbarClock()` | Alpine data | 30s 轮询 /api/status，更新时钟 + next-relay |
| `verifyButton(id)` | Alpine data | 单账号验证按钮状态机（§6.1） |
| `verifyAll(ids)` | Alpine data | 批量验证（§6.3） |
| `seatMap(roomId)` | Alpine data | 座位图轮询渲染（§7.8） |
| `slotsToggle()` | Alpine data | 账号表单 full/custom 切换 |
| `themeInit()` | 全局 | 初始化主题（读 localStorage，默认深色），暴露 `toggleTheme()` |
| `showToast({type,title,desc})` | 全局 | 显示 toast（§5.9） |
| `confirmAction({title,desc,confirmText,danger})` | 全局 | 确认弹层，返回 Promise（§5.10） |
| `prgToast()` | 全局 | 读取 URL flash param（如 ?saved=1）显示对应 toast，清理 URL |

**PRG + Toast 机制**：后端 303 重定向时带 query param（如 `/accounts?saved=1`），前端 Alpine 初始化时读取并 toast"保存成功"，然后 `history.replaceState` 清理 URL。无需后端 flash session。所有 mutating POST 路由的重定向已存在，只需在重定向 URL 上附加 param（这是 routes.py 的微小改动——仅 query string，不动路由结构/字段，视为允许）。

## 9. 响应式

三档断点：
- **≥1280px**：完整布局，220px 侧栏，dashboard 两列。
- **1024–1279px**：侧栏收 64px 图标栏（label 隐藏，hover tooltip），stat-grid 2 列，dashboard 单列。
- **<1024px**：侧栏变顶部汉堡抽屉（Alpine `mobileNav` 控制 translateX），表格转卡片列表，gantt 横向滚动，stat-grid 单列，顶栏简化。

## 10. 可访问性 (A11y) 基线
- 所有交互元素键盘可达，focus 可见（`focus:ring`）。
- Alpine 组件加 `aria-*`：toast `role="alert"`，modal `role="dialog" aria-modal`，loading 按钮 `aria-busy`。
- 颜色对比满足 WCAG AA（深色 `--text-secondary` 对 `--bg-surface`、亮色同理）。
- 图标加 `aria-hidden`，文字 label 优先。

## 11. 测试与验证
- **视觉冒烟**：手动跑一遍 8 页 × 深浅主题（16 组合），检查布局/对比/响应式。
- **构建验证**：`npm run build` 产出 style.css，启动 web 面板确认样式生效。
- **交互验证**：账号验证按钮（单/批量）、删除确认弹层、toast、主题切换、时钟轮询。
- **回归**：确认所有现有路由/表单/API 不变（routes.py 仅 query param 改动）。
- **现有测试**：`tests/test_web_status.py` 应仍通过（/api/status 不变）。

## 12. 风险与缓解
| 风险 | 缓解 |
|---|---|
| Tailwind 构建引入 Node 开发依赖 | 产物入库，部署零 Node；README 更新构建说明 |
| Alpine CDN 依赖（离线/内网） | 评估：若内网无外网，下载 alpine.min.js 入 static/ 本地引用（CDN 优先，本地兜底） |
| 改动面大（8 页 + macro + css + js） | 分阶段：先建设计系统 + shell + 2 样板页，再批量推 6 页 |
| 批量验证串行慢 | 顺序执行避免 Playwright 并发压垮；进度反馈让等待可感知 |
| 删 clock.js/theme.js 改 app.js | app.js 完全覆盖其功能（轮询、主题） |
| 现有 CSS scoping bug（section 8 规则越界） | 整体重写 style.css，根除 |

## 13. 分阶段交付（实现计划骨架）
实现计划（writing-plans 产出）按此顺序：
1. **脚手架**：package.json / tailwind.config.js / src/input.css / .gitignore，跑通 build 产出空 style.css。
2. **设计系统层**：token（colors/typography/spacing/radius/shadow）写进 tailwind.config + input.css @layer components 组件类。
3. **app.js**：Alpine 组件（topbarClock/verifyButton/verifyAll/seatMap/slotsToggle/showToast/confirmAction/prgToast/themeInit）。
4. **base.html + shell macro**：新骨架（sidebar/topbar/toast 容器/Alpine 挂载）。
5. **样板页 1**：dashboard（含 stat/banner/gantt macro 重写）。
6. **样板页 2**：accounts list + form（含验证交互全套）。
7. **批量推 6 页**：targets、user-reserved、coverage、tasks、seats、logs。
8. **routes.py query param 微调**（prgToast 用的 ?saved 等）。
9. **构建 + 冒烟 + 回归**。
