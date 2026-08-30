/* SeatBot app.js — Alpine 组件 + 全局函数。
 * 替代旧 theme.js / clock.js，统一交互行为。
 */
(function () {
  const THEME_KEY = 'seatbot-theme';
  const root = document.documentElement;

  /* ===== 主题：初始化 + 全局切换 ===== */
  function applyTheme(t) {
    root.setAttribute('data-theme', t);
  }
  // 尽早应用主题，避免闪烁（在 <head> 内联或 defer 前执行）
  const saved = (() => { try { return localStorage.getItem(THEME_KEY); } catch (e) { return null; } })();
  if (saved === 'light' || saved === 'dark') applyTheme(saved);
  // 未存偏好时保持 HTML 标签的默认 data-theme="light"

  window.themeInit = function () {
    return {
      isDark: root.getAttribute('data-theme') === 'dark',
      toggle() {
        this.isDark = !this.isDark;
        applyTheme(this.isDark ? 'dark' : 'light');
        try { localStorage.setItem(THEME_KEY, this.isDark ? 'dark' : 'light'); } catch (e) {}
      },
    };
  };

  /* ===== Toast 全局 ===== */
  window.__seatbotToasts = [];
  let toastId = 0;
  window.showToast = function ({ type = 'info', title = '', desc = '' }) {
    const id = ++toastId;
    window.__seatbotToasts.push({ id, type, title, desc });
    document.dispatchEvent(new CustomEvent('toast-new'));
    const ttl = type === 'error' ? 6000 : 4000;
    setTimeout(() => window.dismissToast(id), ttl);
  };
  window.dismissToast = function (id) {
    const i = window.__seatbotToasts.findIndex(t => t.id === id);
    if (i >= 0) window.__seatbotToasts.splice(i, 1);
    document.dispatchEvent(new CustomEvent('toast-new'));
  };

  /* Alpine 组件：toast 容器（挂在 base.html）*/
  window.toastContainer = function () {
    return {
      toasts: [],
      init() {
        const sync = () => { this.toasts = window.__seatbotToasts.slice(); };
        sync();
        document.addEventListener('toast-new', sync);
      },
      dismiss(id) { window.dismissToast(id); },
    };
  };

  /* ===== 确认弹层 ===== */
  let confirmResolver = null;
  window.confirmAction = function ({ title = '确认操作', desc = '', confirmText = '确认', danger = false } = {}) {
    return new Promise((resolve) => {
      confirmResolver = resolve;
      const modal = document.getElementById('confirm-modal');
      if (!modal) { resolve(window.confirm(title)); return; }
      modal.dispatchEvent(new CustomEvent('open', {
        detail: { title, desc, confirmText, danger }
      }));
    });
  };
  window._resolveConfirm = function (val) {
    if (confirmResolver) { confirmResolver(val); confirmResolver = null; }
  };
  window.confirmModal = function () {
    return {
      open: false, title: '', desc: '', confirmText: '确认', danger: false,
      init() {
        document.getElementById('confirm-modal').addEventListener('open', (e) => {
          Object.assign(this, e.detail);
          this.open = true;
        });
      },
      confirm() { this.open = false; window._resolveConfirm(true); },
      cancel()  { this.open = false; window._resolveConfirm(false); },
    };
  };

  /* ===== PRG Toast：读 URL flash param 显示 toast ===== */
  window.prgToast = function () {
    const map = {
      saved: { type: 'success', title: '保存成功' },
      deleted: { type: 'success', title: '已删除' },
      created: { type: 'success', title: '已创建' },
      updated: { type: 'success', title: '已更新' },
      cancelled: { type: 'success', title: '已取消' },
      signed: { type: 'success', title: '已签到' },
      left: { type: 'success', title: '已签退' },
      reserved: { type: 'success', title: '已续约' },
      error: { type: 'error', title: '操作失败' },
    };
    const params = new URLSearchParams(location.search);
    let shown = false;
    for (const [k, v] of params) {
      if (map[k]) {
        // error 携带具体原因时，把它作为 toast 描述
        const t = map[k];
        window.showToast(k === 'error' && v && v !== '1' ? { ...t, desc: v } : t);
        shown = true;
      }
    }
    if (shown && window.history.replaceState) {
      const url = location.pathname + location.hash;
      window.history.replaceState(null, '', url);
    }
  };

  /* ===== 顶栏时钟：轮询 /api/status ===== */
  window.topbarClock = function () {
    return {
      now: '--:--', nextAt: '--:--', nextAcc: '—',
      init() { this.tick(); setInterval(() => this.tick(), 30000); },
      async tick() {
        try {
          const r = await fetch('/api/status', { cache: 'no-store' });
          if (!r.ok) return;
          const j = await r.json();
          this.now = fmtHHMM(new Date(j.now));
          if (j.next_relay_at) {
            this.nextAt = fmtHHMM(new Date(j.next_relay_at));
            this.nextAcc = j.next_relay_account_id || '-';
          } else {
            this.nextAt = '--:--';
            this.nextAcc = '夜间静默';
          }
        } catch (e) { /* keep last */ }
      },
    };
  };
  function fmtHHMM(d) { return d.toTimeString().slice(0, 5); }

  /* ===== 账号验证按钮（单账号）状态机 ===== */
  // 可读失败原因映射
  const ERR_MAP = [
    [/(timeout|timed?\s*out)/i, '登录超时，请重试'],
    [/(password|passwd|密码|账号或密码|401|unauthorized)/i, '手机号或密码错误'],
    [/(risk|风控|vc3|auth cookies|cookie)/i, '登录被风控拦截，可能需手动登录'],
    [/(network|connection|econnrefused|ENOTFOUND)/i, '网络连接失败'],
    [/(not found|account not found)/i, '账号不存在'],
  ];
  function humanizeError(err) {
    if (!err) return '未知错误';
    for (const [re, msg] of ERR_MAP) if (re.test(err)) return msg;
    return err;
  }

  window.verifyButton = function (accountId) {
    return {
      accountId,
      running: false,
      state: 'idle',      // idle | running | success | failed
      message: '',
      get label() {
        if (this.running) return '验证中…';
        if (this.state === 'success') return '已验证';
        return '验证登录';
      },
      async run() {
        if (this.running) return;
        this.running = true; this.state = 'running'; this.message = '';
        try {
          const r = await fetch(`/accounts/${this.accountId}/test-login`, { method: 'POST' });
          const j = await r.json().catch(() => ({}));
          if (r.ok && j.ok) {
            this.state = 'success';
            this.message = '✓ 登录成功';
            window.showToast({ type: 'success', title: '验证通过', desc: this.accountId });
          } else {
            this.state = 'failed';
            this.message = '✗ ' + humanizeError(j.error);
            window.showToast({ type: 'error', title: '验证失败', desc: `${this.accountId} · ${humanizeError(j.error)}` });
          }
        } catch (e) {
          this.state = 'failed';
          this.message = '✗ 网络错误';
          window.showToast({ type: 'error', title: '验证失败', desc: this.accountId + ' · 网络错误' });
        } finally {
          this.running = false;
        }
      },
    };
  };

  /* ===== 批量验证 ===== */
  window.verifyAll = function (accountIds) {
    return {
      ids: accountIds,
      running: false, done: 0, total: accountIds.length,
      failedIds: [],
      get label() {
        if (!this.running) return '全部验证';
        return `验证中 ${this.done}/${this.total}`;
      },
      async run() {
        if (this.running) return;
        this.running = true; this.done = 0; this.failedIds = [];
        for (const id of this.ids) {
          // 串行 fetch 每个账号；若该行有 verifyButton 组件则同步更新其 UI
          let ok = false;
          let errMsg = '';
          try {
            const r = await fetch(`/accounts/${id}/test-login`, { method: 'POST' });
            const j = await r.json().catch(() => ({}));
            ok = r.ok && j.ok;
            if (!ok) errMsg = humanizeError(j.error);
          } catch (e) { errMsg = '网络错误'; }
          if (!ok) this.failedIds.push(id);
          // 同步更新行内 verifyButton 状态（如果该行在 DOM 中）
          const cell = document.querySelector(`[data-verify-id="${id}"]`);
          const comp = cell && cell._x_dataStack && cell._x_dataStack[0];
          if (comp) {
            comp.state = ok ? 'success' : 'failed';
            comp.message = ok ? '✓ 登录成功' : '✗ ' + errMsg;
          }
          this.done++;
          await new Promise(res => setTimeout(res, 400)); // 串行间隔，避免 Playwright 并发
        }
        this.running = false;
        const okCount = this.total - this.failedIds.length;
        window.showToast({
          type: this.failedIds.length ? 'warn' : 'success',
          title: '批量验证完成',
          desc: `${okCount} 通过，${this.failedIds.length} 失败`,
        });
      },
    };
  };

  /* ===== 账号表单：座位×时段矩阵编辑器 ===== */
  /* opts = {seats, initial: {seat: ["HH:MM-HH:MM"]}, others: [{id, seatSlots}],
   *         open: "HH:MM", close: "HH:MM", maxHours: number}
   * 提交前 sync() 把矩阵序列化回后端既有字段 slots/slots_custom/bound_seats/seat_slots,
   * 后端解析逻辑零改动。 */
  window.matrixEditor = function (opts) {
    const toMin = s => { const [h, m] = s.split(':').map(Number); return h * 60 + m; };
    const toHM = v => String(Math.floor(v / 60)).padStart(2, '0') + ':' + String(v % 60).padStart(2, '0');
    const ticks = [];
    for (let t = toMin(opts.open); t <= toMin(opts.close); t += 30) ticks.push(toHM(t));

    return {
      seats: opts.seats,
      rows: opts.seats.map(seat => ({
        seat,
        slots: ((opts.initial && opts.initial[seat]) || []).map(r => {
          const [s, e] = r.split('-');
          return { s, e };
        }),
      })),
      others: opts.others || [],
      maxHours: opts.maxHours,
      ticks,

      validateRow(row) {
        const msgs = [];
        const maxMin = this.maxHours * 60;
        const spans = row.slots.map(x => ({ s: toMin(x.s), e: toMin(x.e) }));
        spans.forEach((x, i) => {
          if (x.e <= x.s) msgs.push('结束需晚于开始');
          if (x.e - x.s > maxMin) msgs.push('单段超 ' + this.maxHours + 'h 上限');
          spans.forEach((y, j) => {
            if (j > i && x.s < y.e && y.s < x.e) msgs.push('时段重叠');
          });
        });
        return msgs;
      },
      get hasErrors() { return this.rows.some(r => this.validateRow(r).length); },
      addSlot(row) {
        /* 每账号每天每座位最多 1 个时段（超星规则） */
        if (row.slots.length >= 1) return;
        row.slots.push({ s: opts.open, e: toHM(Math.min(toMin(opts.open) + 120, toMin(opts.close))) });
      },
      removeSlot(row, i) { row.slots.splice(i, 1); },

      /* 该座位在全部账号合计后的 30min 覆盖位图: mine/other/gap */
      coverage(seat) {
        const open = toMin(opts.open), close = toMin(opts.close), step = 30;
        const mineSlots = (this.rows.find(r => r.seat === seat) || { slots: [] }).slots;
        const bits = [];
        for (let t = open; t < close; t += step) {
          let mine = false;
          mineSlots.forEach(x => { if (toMin(x.s) <= t && t + step <= toMin(x.e)) mine = true; });
          let other = false;
          if (!mine) {
            this.others.forEach(o => (o.seatSlots[seat] || []).forEach(r => {
              const dash = r.indexOf('-');
              const s = r.slice(0, dash), e = r.slice(dash + 1);
              if (toMin(s) <= t && t + step <= toMin(e)) other = true;
            }));
          }
          bits.push(mine ? 'mine' : (other ? 'other' : 'gap'));
        }
        return bits;
      },
      gapsCount(seat) { return this.coverage(seat).filter(b => b === 'gap').length; },

      /* 把矩阵写回隐藏字段; 返回 false 表示有校验错误,调用方应阻止提交 */
      sync() {
        const seatSlots = {};
        this.rows.forEach(r => {
          if (!r.slots.length) return;
          seatSlots[r.seat] = r.slots.map(x => x.s + '-' + x.e);
        });
        const el = document.getElementById('f-seat-slots');
        if (el) el.value = JSON.stringify(seatSlots);
        return !this.hasErrors;
      },
    };
  };

  /* ===== 覆盖图: 数据驱动甘特 + 单元格信息卡 ===== */
  /* 首屏与 /api/dashboard-data 共用同一 JSON 形状, Alpine 响应式渲染,
   * 替代旧的 innerHTML 拼 HTML 字符串实现。30s 轮询节奏不变;
   * 信息卡打开期间跳过甘特重绘, 避免卡片被打断。 */
  const SUCCESS_STATUSES = ['active', 'signed', 'submitting', 'leaving', 'complete'];
  const CARD_STATUSES = ['active', 'signed', 'submitting', 'leaving', 'failed'];

  window.coverageGrid = function (initial) {
    return {
      today: initial.today,
      tomorrow: initial.tomorrow,
      recentLogs: initial.recent_logs || [],
      notifications: initial.notifications || [],
      bootSlots: initial.boot_slots || [],
      targetSeatCount: initial.target_seat_count || 0,
      accountCount: initial.account_count || 0,
      now: initial.now_hhmm || '',
      countdown: 12,
      busy: false,
      timer: null,
      card: null,
      boot: { active: true, display: 0, target: 8, stage: '正在唤醒守护系统…' },

      init() {
        this.timer = setInterval(() => this.tick(), 1000);
        this._initBoot();
        this.refresh();
        this._onVis = () => { if (!document.hidden) this.probe(); };
        document.addEventListener('visibilitychange', this._onVis);
      },
      destroy() {
        clearInterval(this.timer);
        clearInterval(this._bootTimer);
        clearTimeout(this._bootGuard);
        document.removeEventListener('visibilitychange', this._onVis);
      },

      /* 首屏加载动画: 缓动数字向 target 爬升, 首次数据到达后放行到 100% 揭幕 */
      _initBoot() {
        this._bootTimer = setInterval(() => {
          const b = this.boot;
          if (b.display < b.target) {
            b.display = Math.min(b.target, b.display + (b.target - b.display) * 0.07 + 0.25);
            if (b.display >= 99.6 && b.target >= 100) this._finishBoot();
          }
        }, 60);
        setTimeout(() => {
          if (this.boot.target < 88) {
            this.boot.target = 88;
            this.boot.stage = '正在拉取今日与次日占用数据…';
          }
        }, 200);
        this._bootGuard = setTimeout(() => {
          if (this.boot.target < 100) {
            this.boot.target = 100;
            this.boot.stage = '网络较慢，数据稍后自动补齐';
          }
        }, 15000);
      },
      _finishBoot() {
        clearInterval(this._bootTimer);
        this.boot.display = 100;
        this.boot.stage = '就绪';
        setTimeout(() => { this.boot.active = false; }, 320);
      },
      tick() {
        if (this.busy || this.boot.active) return;
        this.countdown = Math.max(0, this.countdown - 1);
        if (this.countdown === 0) this.probe();
      },
      formatCountdown() { return this.busy ? '刷新中…' : '下次检查 ' + this.countdown + 's'; },

      /* 版本探针: 纯本地轻请求; 版本没变就不拉覆盖数据、不打超星 */
      async probe() {
        if (this.busy || this.boot.active) return;
        this.countdown = 12;
        try {
          const r = await fetch('/api/version', { cache: 'no-store' });
          if (!r.ok) return;
          const j = await r.json();
          if (this._knownV === undefined) { this._knownV = j.v; return; }
          if (j.v !== this._knownV) await this.refresh();
        } catch (e) { /* 探针失败静默, 下轮再试 */ }
      },

      cellStatus(c) {
        const hit = c.accounts_info && c.accounts_info[0];
        return hit ? hit.status : 'empty';
      },
      isSuccess(c) { return SUCCESS_STATUSES.includes(this.cellStatus(c)); },
      cellUserMark(c) { return !!c.user_reserved && !this.isSuccess(c); },
      cellOthersMark(c) { return !!c.others_occupied && !this.isSuccess(c); },
      cardHit(c) {
        const hit = (c.accounts_info && c.accounts_info[0]) || null;
        return hit && hit.task_id && CARD_STATUSES.includes(hit.status) ? hit : null;
      },

      openCard(day, seatNum, cell, evt) {
        const hit = this.cardHit(cell);
        if (!hit) return;
        const rect = evt.currentTarget.getBoundingClientRect();
        const width = 280, margin = 12;
        const left = Math.max(margin, Math.min(rect.left, window.innerWidth - width - margin));
        let top = rect.bottom + 8;
        if (top + 240 > window.innerHeight) top = Math.max(margin, rect.top - 248);
        this.card = { day, seatNum, cell, hit, left, top };
      },
      closeCard() { this.card = null; },

      updatedText(hit) {
        if (!hit || !hit.updated_at) return '';
        const d = new Date(hit.updated_at * 1000);
        return '更新于 ' + d.toTimeString().slice(0, 5);
      },

      async refresh() {
        if (this.card) { this.countdown = 10; return; }
        this.busy = true;
        try {
          const r = await fetch('/api/dashboard-data', { cache: 'no-store' });
          if (!r.ok) throw new Error('HTTP ' + r.status);
          const j = await r.json();
          this.today = j.today || this.today;
          this.tomorrow = j.tomorrow || this.tomorrow;
          this.recentLogs = j.recent_logs || [];
          this.notifications = j.notifications || [];
          this.now = j.now_hhmm || this.now;
          if (this.boot.target < 100) this.boot.stage = '核对护城河覆盖…';
        } catch (e) {
          console.warn('dashboard refresh failed:', e);
        } finally {
          this.countdown = 12;
          this.busy = false;
          if (this.boot.target < 100) this.boot.target = 100;
          this._knownV = undefined;  // 拉取后重置基线, 由下轮探针重新对齐
        }
      },
    };
  };
  /* ===== 任务看板: 按状态分列 ===== */
  const BOARD_COLUMNS = [
    { key: 'todo', title: '待执行', statuses: ['pending', 'ready'], tone: 'chip-muted' },
    { key: 'running', title: '进行中', statuses: ['submitting', 'active', 'signed', 'leaving'], tone: 'chip-progress' },
    { key: 'failed', title: '失败', statuses: ['failed'], tone: 'chip-danger' },
    { key: 'done', title: '已完成', statuses: ['complete'], tone: 'chip-success' },
  ];

  window.tasksBoard = function (initial) {
    return {
      day: initial.day,
      tasks: initial.tasks || [],
      columns: BOARD_COLUMNS,
      loading: false,

      ofCol(col) {
        return this.tasks
          .filter(t => col.statuses.includes(t.status))
          .sort((a, b) => a.start.localeCompare(b.start) || a.seat_num.localeCompare(b.seat_num));
      },
      count(col) { return this.ofCol(col).length; },
      toneOf(t) {
        const col = this.columns.find(c => c.statuses.includes(t.status));
        return col ? col.tone : 'chip-muted';
      },
      async refresh() {
        this.loading = true;
        try {
          const r = await fetch('/api/tasks?day=' + this.day, { cache: 'no-store' });
          if (r.ok) { const j = await r.json(); this.tasks = j.tasks || []; }
        } catch (e) {
          console.warn('tasks refresh failed:', e);
        } finally { this.loading = false; }
      },
    };
  };

  /* ===== 日志页：前端级别即时过滤 ===== */
  window.logsFilter = function (initialLevel = '') {
    return { level: initialLevel };
  };

  /* ===== 移动端侧栏开关 ===== */
  window.mobileNav = function () {
    return { open: false, toggle() { this.open = !this.open; } };
  };

  /* 页面加载后跑 PRG toast */
  document.addEventListener('DOMContentLoaded', () => {
    if (window.prgToast) window.prgToast();
  });
})();
