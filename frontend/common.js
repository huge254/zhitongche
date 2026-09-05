// ========== 职通车 common.js ==========
const API = '';   // 同源部署，无需前缀

(async function () {
  const wrap = document.createElement('div');
  wrap.className = 'yz-toast-wrap';
  document.body.appendChild(wrap);

  window.showToast = function (msg, type) {
    const t = document.createElement('div');
    t.className = 'yz-toast ' + (type || 'info');
    t.textContent = msg;
    wrap.appendChild(t);
    requestAnimationFrame(() => t.classList.add('show'));
    setTimeout(() => { t.classList.remove('show'); setTimeout(() => t.remove(), 320); }, 2600);
  };
  window.alert = function (msg) { window.showToast(msg, 'info'); };
})();

async function api(path, options = {}) {
  const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
  const resp = await fetch(API + path, { ...options, headers });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok || data.code !== 200) {
    showToast(data.message || ('请求失败(' + resp.status + ')'), 'error');
    throw new Error(data.message || resp.status);
  }
  return data;
}

function getQuery(name) {
  return new URLSearchParams(location.search).get(name);
}

function renderNav(links = []) {
  const nav = document.querySelector('nav');
  nav.classList.add('yz-nav');
  const cid = localStorage.getItem('companyId');
  const rid = localStorage.getItem('resumeId');
  const right = links.map(l => `<a class="btn btn-outline-light btn-sm me-2" href="${l.href}">${l.text}</a>`).join('');
  const roleLinks = (cid ? '<a class="btn btn-outline-light btn-sm me-2" href="company.html">企业端</a>' : '')
    + (rid ? '<a class="btn btn-outline-light btn-sm me-2" href="resume.html">我的简历</a>' : '')
    + '<a class="btn btn-outline-light btn-sm" href="admin.html">管理后台</a>';
  nav.innerHTML = '<div class="container-fluid">'
    + '<a class="navbar-brand" href="index.html">🚀 职通车 · 求职招聘平台</a>'
    + '<div>' + right + roleLinks + '</div>'
    + '</div>';
}

function renderTable(tableId, list, cols, opsHtml) {
  const thead = '<tr>' + cols.map(c => '<th>' + c.title + '</th>').join('')
    + (opsHtml ? '<th>操作</th>' : '') + '</tr>';
  const tbody = list.map(item =>
    '<tr>' + cols.map(c => '<td>' + (item[c.key] ?? '-') + '</td>').join('')
    + (opsHtml ? opsHtml(item) : '') + '</tr>').join('');
  document.querySelector(tableId + ' thead').innerHTML = thead;
  document.querySelector(tableId + ' tbody').innerHTML = tbody || '<tr><td colspan="99"><div class="yz-empty"><span class="emoji">📭</span>暂无数据</div></td></tr>';
}

// 薪资展示
function salaryText(min, max) {
  if (!min && !max) return '面议';
  const f = v => v >= 1000 ? (v / 1000) + 'k' : v;
  return f(min || 0) + '-' + f(max || 0);
}

// 投递状态徽章
function appStatusBadge(status) {
  const map = {
    0: ['text-bg-secondary', '已投递'],
    1: ['text-bg-info', '初筛'],
    2: ['text-bg-warning', '面试'],
    3: ['text-bg-success', '录用'],
    4: ['text-bg-danger', '淘汰']
  };
  const m = map[status] || ['text-bg-dark', '未知'];
  return '<span class="badge ' + m[0] + '">' + m[1] + '</span>';
}

// 企业状态徽章
function companyStatusBadge(status) {
  const map = { 0: ['text-bg-warning', '待审核'], 1: ['text-bg-success', '已通过'], 2: ['text-bg-danger', '已拒绝'] };
  const m = map[status] || ['text-bg-dark', '未知'];
  return '<span class="badge ' + m[0] + '">' + m[1] + '</span>';
}
