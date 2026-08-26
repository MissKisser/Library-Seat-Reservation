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
  // 否则保持 HTML 标签的默认 data-theme="dark"

  window.themeInit = function () {
    return {
      isDark: root.getAttribute('data-theme') !== 'light',
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

  /* ===== 账号表单：full/custom 切换 ===== */
  window.slotsToggle = function (initial = 'full') {
    return {
      mode: initial,
      isCustom() { return this.mode === 'custom'; },
    };
  };

  /* ===== 座位图：轮询渲染 ===== */
  window.seatMap = function (roomId) {
    return {
      roomId, sections: [], error: null, loading: true,
      init() {
        this.load();
        this.timer = setInterval(() => this.load(), 30000);
      },
      destroy() { clearInterval(this.timer); },
      async load() {
        try {
          const r = await fetch(`/api/seats/${this.roomId}`, { cache: 'no-store' });
          const j = await r.json().catch(() => ({}));
          if (j.error) { this.error = j.error; this.sections = []; }
          else if (j.by_target) {
            this.error = null;
            this.sections = Object.entries(j.by_target).map(([seat, seats]) => ({
              seat, seats: seats || [],
            }));
          } else {
            this.error = '无数据';
            this.sections = [];
          }
        } catch (e) {
          this.error = '获取座位失败';
        } finally {
          this.loading = false;
        }
      },
    };
  };

  /* ===== Dashboard 局部刷新: 每 30s 拉一次 /api/dashboard-data ===== */
  /* 替换之前每 30s location.reload() 的整页刷新。整页刷新会丢 gantt 上
   * 未提交的 hover / popover 状态、输入框内容、滚动位置,且对 server 压力大。
   * 这里只局部重渲染三块:
   *   1. 状态 banner (occ_err)
   *   2. Gantt <tbody> (含 cell 着色 + popover)
   *   3. "最近活动" feed
   * 其它 (侧栏、顶栏时钟、stat 卡计数) 不动。
   */
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }
  function popoverHtml(c, hit, seatNum) {
    /* 复制 templates/_macros/popover.html 的 cell_popover 结构 (POST forms) */
    const taskId = hit.task_id;
    const status = hit.status;
    const accId = hit.id || '';
    const st = c.start, en = c.end;
    if (status === 'active') {
      return `
        <form method="post" action="/tasks/${taskId}/sign"><button type="submit">立即签到</button></form>
        <form method="post" action="/tasks/${taskId}/leave"><button type="submit">立即签退</button></form>`;
    }
    if (status === 'failed') {
      return `<form method="post" action="/tasks/quick-reserve">
        <input type="hidden" name="account_id" value="${escapeHtml(accId)}">
        <input type="hidden" name="seat_num" value="${escapeHtml(seatNum)}">
        <input type="hidden" name="start" value="${escapeHtml(st)}">
        <input type="hidden" name="end" value="${escapeHtml(en)}">
        <button type="submit" class="text-accent">续约该段</button>
      </form>`;
    }
    if (taskId) {
      return `<button @click="open=false; confirmAction({title:'确认取消？',desc:'取消该预约时段',confirmText:'取消预约',danger:true}).then(ok=>{if(ok){const f=document.createElement('form');f.method='post';f.action='/tasks/${taskId}/cancel';document.body.appendChild(f);f.submit();}})">取消</button>`;
    }
    return '';
  }
  function ganttRowsHtml(rows) {
    if (!rows || !rows.length) {
      return `<tr><td colspan="99" class="text-center text-ink-muted p-4">暂无目标座位</td></tr>`;
    }
    const headCells = rows[0].cells.map(c =>
      `<th>${escapeHtml(c.start)}</th>`).join('');
    const bodyRows = rows.map(r => {
      const cells = r.cells.map(c => {
        const hit = c.accounts_info && c.accounts_info[0] || null;
        const status = hit ? hit.status : 'empty';
        const actionable = hit && ['active','submitting','failed','leaving'].includes(status);
        const classes = ['cell-' + status];
        if (actionable) classes.push('cell-actionable');
        if (c.user_reserved) classes.push('cell-user-reserved');
        if (c.others_occupied) classes.push('cell-others-occupied');
        const titleParts = [`${r.seat_num} · ${c.start}-${c.end}`];
        if (c.user_reserved) titleParts.push('👤 用户硬预约');
        if (c.others_occupied) titleParts.push('🔒 他人已占');
        if (hit) titleParts.push(`${hit.id || ''} · ${status}`);
        let inner = '';
        if (hit && ['active','submitting','leaving','failed'].includes(status) && hit.task_id) {
          inner = `<div class="relative inline-flex w-full h-full" x-data="{ open: false }" @click.outside="open=false">
            <button class="w-full h-full" @click="open=!open" aria-label="操作"></button>
            <div class="popover" x-show="open" x-cloak style="display:none">${popoverHtml(c, hit, r.seat_num)}</div>
          </div>`;
        } else {
          if (c.user_reserved) inner = '<span class="text-xs">👤</span>';
          else if (c.others_occupied) inner = '<span class="text-xs">🔒</span>';
        }
        return `<td class="cell ${classes.join(' ')}" title="${escapeHtml(titleParts.join(' · '))}">${inner}</td>`;
      }).join('');
      const labelHtml = `<span class="font-mono text-xs">${escapeHtml(r.seat_num)}</span>` +
        (r.label ? `<span class="text-xs text-ink-muted ml-1">${escapeHtml(r.label)}</span>` : '');
      return `<tr><th class="row-label">${labelHtml}</th>${cells}</tr>`;
    }).join('');
    return `<tr><th class="row-label">座位 \\ 时段</th>${headCells}</tr>${bodyRows}`;
  }
  function bannerHtml(occErr, gapCount, rowCount) {
    if (!rowCount) return '<div class="banner banner-warn">尚未设置目标座位。<a class="text-accent" href="/targets">去添加 →</a></div>';
    if (gapCount) return `<div class="banner banner-error">检测到 <b>${gapCount}</b> 个时段空缺，护城河有缺口。</div>`;
    if (occErr) return `<div class="banner banner-warn">无法获取超星他人占用数据：${escapeHtml(occErr)}</div>`;
    return '<div class="banner banner-ok">护城河稳固，全部目标座位已覆盖。</div>';
  }
  function recentLogsHtml(logs) {
    if (!logs || !logs.length) return '<div class="text-xs text-ink-muted">暂无活动</div>';
    return logs.slice(0, 8).map(l => {
      const dotCls = l.level === 'ERROR' ? 'bg-danger' : l.level === 'WARN' ? 'bg-warn' : 'bg-info';
      return `<div class="flex items-start gap-2 text-xs">
        <span class="mt-1 h-1.5 w-1.5 rounded-full shrink-0 ${dotCls}"></span>
        <div class="flex-1 min-w-0">
          <div class="text-ink-secondary truncate">${escapeHtml(l.message || '')}</div>
          <div class="text-ink-muted">${escapeHtml(l.account_id || '—')}</div>
        </div>
      </div>`;
    }).join('');
  }
  function statHtml(value, hint, accent) {
    const valCls = accent ? 'stat-value accent' : 'stat-value';
    return `<div class="stat-card">
      <div class="stat-label"></div>
      <div class="${valCls}">${escapeHtml(String(value))}</div>
      ${hint ? `<div class="stat-hint">${escapeHtml(String(hint))}</div>` : ''}
    </div>`;
  }

  window.dashboardRefresh = function (initialRows) {
    return {
      countdown: 30,
      busy: false,
      rows: initialRows,
      occErr: null,
      gapCount: 0,
      recentLogs: [],
      targetSeatCount: 0,
      accountCount: 0,
      timer: null,
      init() {
        // Alpine 3 会自动调用 init(),不需要 x-init (避免 init 双调用 bug)
        this.timer = setInterval(() => this.tick(), 1000);
      },
      destroy() { clearInterval(this.timer); },
      tick() {
        if (this.busy) return;
        this.countdown = Math.max(0, this.countdown - 1);
        if (this.countdown === 0) {
          this.refresh();
        }
      },
      async refresh() {
        this.busy = true;
        try {
          const r = await fetch('/api/dashboard-data', { cache: 'no-store' });
          if (!r.ok) throw new Error('HTTP ' + r.status);
          const j = await r.json();
          this.rows = j.rows || [];
          this.recentLogs = j.recent_logs || [];
          this.occErr = j.occ_err || null;
          this.gapCount = j.gap_count || 0;
          this.targetSeatCount = j.target_seat_count || 0;
          this.accountCount = j.account_count || 0;
          this.applyToDom();
        } catch (e) {
          console.warn('dashboard refresh failed:', e);
        } finally {
          this.countdown = 30;
          this.busy = false;
        }
      },
      applyToDom() {
        const gantt = document.getElementById('dashboard-gantt');
        if (!gantt) return;
        const ganttBody = gantt.querySelector('tbody');
        const ganttHead = gantt.querySelector('thead');
        const banner = document.getElementById('dashboard-banner');
        const logsBox = document.getElementById('dashboard-recent-logs');
        const statsBox = document.getElementById('dashboard-stats');
        if (ganttHead && ganttBody && this.rows.length) {
          const head = `<tr><th class="row-label">座位 \\ 时段</th>${
            this.rows[0].cells.map(c => `<th>${escapeHtml(c.start)}</th>`).join('')
          }</tr>`;
          ganttHead.innerHTML = head;
          ganttBody.innerHTML = ganttRowsHtml(this.rows);
        } else if (ganttBody) {
          ganttBody.innerHTML = `<tr><td colspan="99" class="text-center text-ink-muted p-4">暂无目标座位</td></tr>`;
        }
        if (banner) {
          banner.innerHTML = bannerHtml(this.occErr, this.gapCount, this.rows.length);
        }
        if (logsBox) {
          logsBox.innerHTML = recentLogsHtml(this.recentLogs);
        }
        if (statsBox) {
          // 4 张 stat 卡:目标座位 / 守护账号 / 今日覆盖 (rows.length 座, gap_count 个空缺) / 当前时间
          statsBox.innerHTML = statHtml(this.targetSeatCount, null, false) +
            statHtml(this.accountCount, null, false) +
            statHtml(this.rows.length + ' 座',
              this.gapCount ? this.gapCount + ' 个空缺' : '全覆盖',
              this.gapCount > 0) +
            statHtml(new Date().toTimeString().slice(0, 5), null, false);
        }
      },
      formatCountdown() {
        return '下次刷新 ' + this.countdown + 's';
      },
    };
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
