/* ═══ novel_agent WebUI — Vue 3 全局构建,无构建步骤 ═══ */
'use strict';

if (!window.Vue) {
  document.querySelector('.nojs').style.display = 'block';
  throw new Error('Vue 未加载');
}

const { createApp, reactive, computed, ref, nextTick, onMounted, toRefs } = Vue;

/* ── 引擎预设(与 config.py 保持一致) ─────────────────── */
const GENRES = ["玄幻", "仙侠", "科幻", "历史", "都市", "悬疑", "游戏", "末世"];
const STYLES = ["番茄爆款", "科幻硬核", "轻松日常", "热血战斗", "悬疑烧脑"];
const PERSONS = ["第一人称", "第三人称", "第三人称有限视角（跟随主角）"];
const SLANGS = ["无", "低", "中", "高"];
const LIB_LABELS = { characters: "人物", geography: "地理", history: "历史",
                     power_system: "战力", factions: "势力" };

/* 任务阶段定义(与 pipelines.py 事件一致) */
const NEW_BOOK_STEPS = [
  { key: "title",   label: "① 书名简介", detail: "AI 构思" },
  { key: "settings", label: "② 设定库", detail: "5 大子库" },
  { key: "outline", label: "③ 第一卷粗纲", detail: "叙事脉络" },
];
const WRITE_STEPS = [
  { key: "outline",        label: "① 细纲", detail: "任务式 checklist" },
  { key: "outline_review", label: "② 小纲审查", detail: "8 维度打分" },
  { key: "writing",        label: "③ 章节写作", detail: "" },
  { key: "content_review", label: "④ 正文校验", detail: "8 维度匹配" },
  { key: "maintenance",    label: "⑤ 设定维护", detail: "提取入库" },
];

/* ── 全局状态 ──────────────────────────────────────────── */
const state = reactive({
  ready: false,
  page: 'shelf',
  health: { api_key_configured: true },
  books: [],
  currentBook: null,      // BookInfo(dir_name/title/…)
  bookDetail: null,       // detail: timeline + hooks
  // 任务
  activeTask: null,       // {id,kind,label,status}
  taskStage: '',
  taskStageLabel: '',
  taskLogs: [],
  partialText: '',
  overlayOpen: false,
  ws: null,
  // 写作台
  selectedChapter: 0,
  chapter: null,          // 当前章正文
  settingsData: null,     // 设定库速查
  settingsTab: 'characters',
  // 设定库页
  libPage: 'characters',
  libSelected: null,
});

/* ── API 封装 ──────────────────────────────────────────── */
async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    throw new Error(body.detail || r.statusText);
  }
  return r.status === 204 ? null : r.json();
}

/* ── WebSocket ─────────────────────────────────────────── */
let wsRetry = 0;
function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => { wsRetry = 0; };
  ws.onmessage = (m) => handleEvent(JSON.parse(m.data));
  ws.onclose = () => {
    wsRetry = Math.min(wsRetry + 1, 5);
    setTimeout(connectWS, 500 * Math.pow(2, wsRetry));
  };
  state.ws = ws;
}

function handleEvent(e) {
  if (e.type === 'task_started') {
    state.activeTask = { id: e.task_id, kind: e.kind, label: e.label, status: 'running' };
    state.taskLogs = []; state.taskStage = ''; state.taskStageLabel = '';
    state.partialText = '';
    return;
  }
  if (!state.activeTask || e.task_id !== state.activeTask.id) return;
  switch (e.type) {
    case 'stage':
      state.taskStage = e.stage;
      state.taskStageLabel = e.label;
      state.taskLogs.push(`▶ ${e.label}${e.detail ? '（' + e.detail + '）' : ''}`);
      break;
    case 'log':
      state.taskLogs.push(e.text);
      break;
    case 'warning':
      state.taskLogs.push('⚠ ' + e.text);
      break;
    case 'partial':
      state.partialText = e.text;
      break;
    case 'error':
      state.activeTask.status = 'failed';
      state.taskLogs.push('✗ ' + e.message);
      state.overlayOpen = true;
      break;
    case 'task_done': {
      state.activeTask.status = 'done';
      state.taskStageLabel = '完成';
      refreshBooks();
      // 写完新章后跳到最新章
      if (state.activeTask.kind === 'write_chapter') state.selectedChapter = 0;
      if (state.currentBook) loadBook(state.currentBook.dir_name);
      break;
    }
  }
  nextTick(() => {
    const el = document.querySelector('.logbox');
    if (el) el.scrollTop = el.scrollHeight;
  });
}

/* ── 数据加载 ──────────────────────────────────────────── */
async function refreshBooks() {
  try { state.books = await api('/api/books'); } catch (e) { console.error(e); }
}

async function loadBook(dirName) {
  try {
    state.bookDetail = await api(`/api/books/${encodeURIComponent(dirName)}`);
    state.currentBook = state.books.find(b => b.dir_name === dirName)
      || { dir_name: dirName, title: state.bookDetail.title };
    localStorage.setItem('na.currentBook', dirName);
    loadChapterMeta();
    loadSettings();
  } catch (e) {
    console.error(e);
    state.currentBook = null;
    state.bookDetail = null;
    localStorage.removeItem('na.currentBook');
  }
}

async function openBook(dirName) {
  try { await api(`/api/books/${encodeURIComponent(dirName)}/open`, { method: 'POST' }); } catch (e) { /* 忽略 */ }
  await loadBook(dirName);
  go('workbench');
}

/* 页面跳转 */
function go(page) {
  state.page = page;
  if (page === 'shelf') refreshBooks();
}

/* 写作台数据加载 */
async function loadChapterMeta() {
  const tl = (state.bookDetail && state.bookDetail.timeline) || [];
  if (!tl.length) { state.selectedChapter = 0; state.chapter = null; return; }
  const nums = tl.map(c => c.num);
  if (!nums.includes(state.selectedChapter)) state.selectedChapter = nums[nums.length - 1];
  await selectChapter(state.selectedChapter);
}

async function selectChapter(num) {
  if (!state.currentBook) return;
  state.selectedChapter = num;
  state.chapter = null;
  try {
    state.chapter = await api(
      `/api/books/${encodeURIComponent(state.currentBook.dir_name)}/chapters/${num}`);
  } catch (e) { state.chapter = null; }
}

async function loadSettings() {
  if (!state.currentBook) return;
  try {
    state.settingsData = await api(
      `/api/books/${encodeURIComponent(state.currentBook.dir_name)}/settings`);
  } catch (e) { state.settingsData = null; }
}

/* 设定条目摘要(右栏速查用) */
function entrySummary(e) {
  return e.summary || e.current_status || e.personality || e.description
    || e.overview || e.content || '';
}

const IMPORTANCE_LABELS = { core: '核心', supporting: '支撑', minor: '次要' };

/* ── 任务触发 ──────────────────────────────────────────── */
async function writeNextChapter() {
  if (!state.currentBook || state.activeTask) return;
  try {
    const task = await api('/api/tasks/write-chapter', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ dir_name: state.currentBook.dir_name }),
    });
    state.activeTask = task;
    state.overlayOpen = true;
    go('workbench');
  } catch (e) { alert(e.message); }
}

/* ═══════════════════════════════════════════════════════
   页面组件
   ═══════════════════════════════════════════════════════ */

/* ── 书架页 ────────────────────────────────────────────── */
const ShelfPage = {
  data() {
    return {
      wizard: false,
      submitting: false,
      error: '',
      genres: GENRES, styles: STYLES, persons: PERSONS, slangs: SLANGS,
      form: {
        name: '', idea: '', genre: GENRES[0], person: PERSONS[1],
        style: STYLES[0], slang: '中', words: 3000, core_setting: '',
      },
    };
  },
  computed: {
    books: () => state.books,
  },
  methods: {
    fmtWords(n) { return n >= 10000 ? (n / 10000).toFixed(1) + ' 万字' : n + ' 字'; },
    async open(b) { await openBook(b.dir_name); },
    async submit() {
      if (!this.form.idea.trim()) { this.error = '请填写核心脑洞'; return; }
      this.submitting = true; this.error = '';
      try {
        const task = await api('/api/tasks/new-book', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(this.form),
        });
        state.activeTask = task;
        state.overlayOpen = true;
        this.wizard = false;
      } catch (e) { this.error = e.message; }
      this.submitting = false;
    },
  },
  template: `
  <div>
    <div class="shelf-head">
      <h2>📚 书架</h2>
      <span class="spacer"></span>
      <button class="btn primary" @click="wizard = true">＋ 新建书</button>
    </div>

    <div v-if="!books.length" class="empty">
      <div class="big">📖</div>
      书架还是空的<br>
      点右上角「新建书」开始你的第一部小说
    </div>

    <div class="book-grid">
      <div v-for="b in books" :key="b.dir_name" class="book-card" @click="open(b)">
        <h3>{{ b.title }} <span class="fmt">{{ b.format_label }}</span></h3>
        <div>
          <span class="tag brand">{{ b.genre || '未分类' }}</span>
          <span class="tag" v-if="b.style">{{ b.style }}</span>
          <span class="tag" v-if="b.unresolved_hooks" title="未回收伏笔">{{ b.unresolved_hooks }} 伏笔</span>
        </div>
        <div class="idea">{{ b.core_idea }}</div>
        <div class="meta">
          <span><b>{{ b.chapters_written }}</b> 章</span>
          <span><b>{{ fmtWords(b.total_words) }}</b></span>
          <span v-if="b.updated_at">更新于 {{ b.updated_at }}</span>
        </div>
      </div>
    </div>

    <!-- 新建书向导 -->
    <div v-if="wizard" class="overlay" @click.self="wizard = false">
      <div class="overlay-card">
        <h2>✨ 新建书</h2>
        <div class="form-grid">
          <div class="field full">
            <label>核心脑洞 <span class="req">*</span></label>
            <textarea v-model="form.idea" rows="4" placeholder="例:主角穿越到全民觉醒的异世界,觉醒的能力是能复制别人的天赋……（越具体越好）"></textarea>
          </div>
          <div class="field">
            <label>自定义书名（留空由 AI 起）</label>
            <input v-model="form.name" placeholder="留空 = AI 起 3 个候选择优">
          </div>
          <div class="field">
            <label>故事分类</label>
            <select v-model="form.genre"><option v-for="g in genres" :key="g">{{ g }}</option></select>
          </div>
          <div class="field">
            <label>文风</label>
            <select v-model="form.style"><option v-for="s in styles" :key="s">{{ s }}</option></select>
          </div>
          <div class="field">
            <label>叙事人称</label>
            <select v-model="form.person"><option v-for="p in persons" :key="p">{{ p }}</option></select>
          </div>
          <div class="field">
            <label>网感程度</label>
            <select v-model="form.slang"><option v-for="s in slangs" :key="s">{{ s }}</option></select>
          </div>
          <div class="field">
            <label>每章字数</label>
            <input v-model.number="form.words" type="number" min="1000" max="8000" step="500">
          </div>
          <div class="field full">
            <label>核心设定补充（可选）</label>
            <textarea v-model="form.core_setting" rows="2" placeholder="例:力量体系为斗气九段;大陆名为玄黄大陆……"></textarea>
          </div>
        </div>
        <div v-if="error" class="form-err">{{ error }}</div>
        <div class="overlay-actions">
          <button class="btn ghost" @click="wizard = false">取消</button>
          <button class="btn primary" :disabled="submitting" @click="submit">
            {{ submitting ? '提交中…' : '开始创建（约 3-6 分钟）' }}
          </button>
        </div>
      </div>
    </div>
  </div>`,
};

/* ── 写作台(三栏) ──────────────────────────────────────── */
const WorkbenchPage = {
  computed: {
    book: () => state.currentBook,
    detail: () => state.bookDetail,
    timeline: () => (state.bookDetail && state.bookDetail.timeline) || [],
    chapter: () => state.chapter,
    writing: () => state.activeTask && state.activeTask.kind === 'write_chapter'
      && state.activeTask.status === 'running',
    writingSteps: () => WRITE_STEPS,
    writingStepIndex() {
      const i = WRITE_STEPS.findIndex(s => s.key === state.taskStage);
      return i < 0 ? 0 : i;
    },
    partial: () => state.partialText,
    libEntries() {
      const d = state.settingsData;
      return (d && d[state.settingsTab]) || [];
    },
    libTabs: () => Object.entries(LIB_LABELS).map(([key, label]) => ({ key, label })),
    settingsTab: () => state.settingsTab,
    hooks: () => (state.bookDetail && state.bookDetail.hooks) || [],
    totalWords() {
      const n = (state.currentBook && state.currentBook.total_words) || 0;
      return n >= 10000 ? (n / 10000).toFixed(1) + ' 万字' : n + ' 字';
    },
  },
  methods: {
    selectChapter(num) { return selectChapter(num); },
    setTab(key) { state.settingsTab = key; },
    entrySummary,
    importanceLabel: (imp) => IMPORTANCE_LABELS[imp] || '',
  },
  template: `
  <div>
    <div v-if="!book" class="empty">
      <div class="big">✍️</div>
      请先从书架选择一本书
    </div>

    <div v-else class="workbench">
      <!-- 左栏:章节树 -->
      <div class="col-l">
        <div class="sec-title">章节</div>
        <div class="vol">第一卷 · 已 {{ timeline.length }} 章</div>
        <div v-for="c in timeline" :key="c.num" class="chap"
             :class="{on: c.num === chapter?.num}" @click="selectChapter(c.num)">
          {{ String(c.num).padStart(2, '0') }} {{ c.title }}
          <span class="st done">✓</span>
        </div>
        <div class="chap" style="color:#bbb">
          {{ String(timeline.length + 1).padStart(2, '0') }} 待写
          <span class="st run" v-if="writing">⟳</span>
        </div>
        <div class="sec-title" style="margin-top:16px">本书统计</div>
        <div class="wb-stats">
          已写 <b>{{ book.chapters_written }}</b> 章<br>
          累计 <b>{{ totalWords }}</b><br>
          未回收伏笔 <b :style="hooks.length ? 'color:#8a6100' : ''">{{ hooks.length }}</b>
        </div>
      </div>

      <!-- 中栏:流水线 + 正文 -->
      <div class="col-m">
        <div v-if="writing" class="steps">
          <div v-for="(st, i) in writingSteps" :key="st.key" class="step"
               :class="{done: writingStepIndex > i, now: writingStepIndex === i}">
            {{ st.label }}<small v-if="st.detail">{{ st.detail }}</small>
          </div>
        </div>

        <template v-if="chapter">
          <div class="chapter-head">
            <h3>第 {{ chapter.num }} 章 · {{ chapter.title }}</h3>
            <span class="meta">{{ chapter.content.length }} 字</span>
            <span class="meta" v-if="chapter.summary">摘要:{{ chapter.summary.slice(0, 40) }}…</span>
          </div>
          <div class="chapter-content">{{ chapter.content }}</div>
        </template>
        <div v-else-if="writing && partial" class="chapter-content">{{ partial }}</div>
        <div v-else class="empty">
          <div class="big">📖</div>
          <template v-if="writing">AI 正在写作…</template>
          <template v-else-if="timeline.length">选择左侧章节阅读</template>
          <template v-else>
            这本书还没有章节<br>点右上「▶ 写下一章」开始创作
          </template>
        </div>
      </div>

      <!-- 右栏:设定速查 + 伏笔 -->
      <div class="col-r">
        <div class="panel">
          <div class="tabs">
            <span v-for="t in libTabs" :key="t.key" class="tab"
                  :class="{on: settingsTab === t.key}" @click="setTab(t.key)">{{ t.label }}</span>
          </div>
          <div v-if="!libEntries.length" style="color:#999;font-size:12px">（空）</div>
          <div v-for="e in libEntries" :key="e._name" class="entry">
            <b>{{ e._name }}</b>
            <span v-if="e.importance" class="tag">{{ importanceLabel(e.importance) }}</span>
            <p v-if="entrySummary(e)">{{ entrySummary(e).slice(0, 60) }}</p>
          </div>
        </div>
        <div class="panel">
          <div class="sec-title" style="margin-top:0">伏笔 · {{ hooks.length }} 未回收</div>
          <div v-if="!hooks.length" style="color:#999;font-size:12px">全部伏笔已回收 ✓</div>
          <div v-for="(h, i) in hooks" :key="i" class="fb">
            <span class="dot open"></span><p>{{ h }}</p>
          </div>
        </div>
      </div>
    </div>
  </div>`,
};
/* ═══════════════════════════════════════════════════════
   Task 8: 设定库 / 伏笔 / 评测 / 导出页
   ═══════════════════════════════════════════════════════ */

const FIELD_LABELS = {
  name: '名称', aliases: '别名/称号', gender: '性别', age: '年龄',
  appearance: '外貌', personality: '性格', background: '背景故事',
  abilities: '能力/技能', relationships: '关系网', current_status: '当前状态',
  first_appearance_chapter: '首次出场章', importance: '重要性',
  last_active_chapter: '最后活跃章', notes: '备注',
  type: '类型', description: '描述', significance: '重要性说明',
  related_factions: '关联势力', related_characters: '关联人物',
  first_mentioned_chapter: '首次提及章',
  time_period: '发生时期', impact: '对当今的影响', revealed_in_chapter: '揭示于章',
  category: '分类', levels: '境界列表', basic_info: '基础设定',
  advanced_info: '高级设定', special_cases: '特殊情况', first_explained_chapter: '首次说明章',
  leader: '首领', key_members: '核心成员', territory: '势力范围',
};

function fmtVal(v) {
  if (Array.isArray(v)) {
    if (!v.length) return '';
    return v.map(x => {
      if (x && typeof x === 'object') return x.name ? `${x.name}${x.description ? '：' + x.description : ''}` : JSON.stringify(x);
      return String(x);
    }).join('；');
  }
  if (v && typeof v === 'object') {
    return Object.entries(v).map(([k, val]) => `${k}：${val}`).join('；');
  }
  return String(v);
}

function detailRows(e) {
  const rows = [];
  for (const [k, v] of Object.entries(e)) {
    if (k === '_name' || v === null || v === '' || (Array.isArray(v) && !v.length)) continue;
    if (v && typeof v === 'object' && !Array.isArray(v) && !Object.keys(v).length) continue;
    rows.push({ key: FIELD_LABELS[k] || k, val: fmtVal(v) });
  }
  return rows;
}

/* ── 设定库页 ──────────────────────────────────────────── */
const SettingsPage = {
  computed: {
    book: () => state.currentBook,
    data: () => state.settingsData,
    libPage: () => state.libPage,
    libSelected: () => state.libSelected,
    libTabs() {
      const d = state.settingsData || {};
      return Object.entries(LIB_LABELS).map(([key, label]) => ({
        key, label, count: (d[key] || []).length,
      }));
    },
    entries() { return (state.settingsData && state.settingsData[state.libPage]) || []; },
    selectedEntry() {
      return this.entries.find(e => e._name === state.libSelected) || null;
    },
    rows() { return this.selectedEntry ? detailRows(this.selectedEntry) : []; },
  },
  methods: {
    setLib(key) {
      state.libPage = key;
      state.libSelected = null;
    },
    pick(name) { state.libSelected = name; },
    importanceLabel: (imp) => IMPORTANCE_LABELS[imp] || '',
    ensureLoaded() {
      if (!state.settingsData && state.currentBook) loadSettings();
    },
  },
  mounted() { this.ensureLoaded(); },
  template: `
  <div>
    <div v-if="!book" class="empty"><div class="big">🏛</div>请先从书架选择一本书</div>
    <div v-else class="detail-layout">
      <div class="detail-list">
        <div class="panel">
          <div class="sec-title" style="margin-top:0">设定库 · 5 大子库</div>
          <div v-for="t in libTabs" :key="t.key" class="chap"
               :class="{on: libPage === t.key}" @click="setLib(t.key)">
            {{ t.label }} <span class="st">{{ t.count }}</span>
          </div>
        </div>
        <div class="panel">
          <div class="sec-title" style="margin-top:0">{{ libTabs.find(t=>t.key===libPage)?.label }}条目</div>
          <div v-if="!entries.length" style="color:#999;font-size:12px">（空）</div>
          <div v-for="e in entries" :key="e._name" class="chap"
               :class="{on: libSelected === e._name}" @click="pick(e._name)">
            {{ e._name }}
            <span v-if="e.importance" class="tag" style="margin-left:auto">{{ importanceLabel(e.importance) }}</span>
          </div>
        </div>
      </div>
      <div class="detail-view">
        <div v-if="!selectedEntry" class="empty">
          <div class="big">📄</div>左侧选择一个条目查看百科式详情
        </div>
        <div v-else class="panel">
          <div class="chapter-head">
            <h3>{{ selectedEntry._name }}</h3>
            <span v-if="selectedEntry.importance" class="tag brand">{{ importanceLabel(selectedEntry.importance) }}</span>
          </div>
          <table class="kv-table">
            <tr v-for="r in rows" :key="r.key">
              <th>{{ r.key }}</th><td>{{ r.val }}</td>
            </tr>
          </table>
        </div>
      </div>
    </div>
  </div>`,
};

/* ── 伏笔追踪页 ────────────────────────────────────────── */
const HooksPage = {
  computed: {
    book: () => state.currentBook,
    hooks: () => (state.bookDetail && state.bookDetail.hooks) || [],
  },
  template: `
  <div>
    <div v-if="!book" class="empty"><div class="big">🧵</div>请先从书架选择一本书</div>
    <div v-else>
      <div class="shelf-head">
        <h2>🧵 伏笔追踪 · 《{{ book.title }}》</h2>
        <span class="spacer"></span>
        <span class="tag" :class="{'brand': hooks.length}">{{ hooks.length }} 条未回收</span>
      </div>
      <div class="panel hooks-list">
        <div v-if="!hooks.length" style="color:#147a4e;padding:20px 0;text-align:center">
          ✓ 全部伏笔已回收
        </div>
        <div v-for="(h, i) in hooks" :key="i" class="fb">
          <span class="dot open"></span>
          <p>{{ h }}<br><small>#{{ i + 1 }}</small></p>
        </div>
      </div>
    </div>
  </div>`,
};

/* ── 评测报告页(字数趋势 SVG) ──────────────────────────── */
const ReportPage = {
  computed: {
    book: () => state.currentBook,
    timeline: () => (state.bookDetail && state.bookDetail.timeline) || [],
    avgWords() {
      if (!this.timeline.length) return 0;
      return Math.round(this.timeline.reduce((s, c) => s + (c.words || 0), 0) / this.timeline.length);
    },
    bars() {
      const tl = this.timeline;
      if (!tl.length) return '';
      const W = 700, H = 180, PAD = 24;
      const max = Math.max(...tl.map(c => c.words || 0), 1);
      const bw = Math.max(6, Math.min(36, (W - PAD * 2) / tl.length - 6));
      const step = (W - PAD * 2) / tl.length;
      return tl.map((c, i) => {
        const h = Math.max(3, ((c.words || 0) / max) * (H - 50));
        const x = PAD + i * step + (step - bw) / 2;
        const y = H - 24 - h;
        return `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${bw.toFixed(1)}" height="${h.toFixed(1)}" rx="3" fill="#4B3FE3"><title>第${c.num}章 ${c.title} · ${c.words}字</title></rect>`
          + `<text x="${(PAD + i * step + step / 2).toFixed(1)}" y="${H - 8}" font-size="9" fill="#999" text-anchor="middle">${c.num}</text>`;
      }).join('');
    },
  },
  template: `
  <div>
    <div v-if="!book" class="empty"><div class="big">📊</div>请先从书架选择一本书</div>
    <div v-else>
      <div class="shelf-head">
        <h2>📊 评测报告 · 《{{ book.title }}》</h2>
      </div>
      <div class="panel">
        <div class="sec-title" style="margin-top:0">章节字数趋势（均章 {{ avgWords }} 字）</div>
        <div v-if="!timeline.length" style="color:#999">还没有章节</div>
        <svg v-else viewBox="0 0 700 180" style="width:100%;max-width:760px">
          <g v-html="bars"></g>
        </svg>
      </div>
      <div class="panel">
        <div class="sec-title" style="margin-top:0">章节一览</div>
        <table class="kv-table">
          <tr><th>章</th><th>标题</th><th>字数</th><th>摘要</th></tr>
          <tr v-for="c in timeline" :key="c.num">
            <th>第 {{ c.num }} 章</th>
            <td>{{ c.title }}</td>
            <td>{{ c.words }}</td>
            <td style="color:#888">{{ (c.summary || '').slice(0, 50) }}{{ (c.summary || '').length > 50 ? '…' : '' }}</td>
          </tr>
        </table>
        <div style="color:#999;font-size:12px;margin-top:10px">
          注：细纲审查分 / 正文校验分在生成日志中查看（见任务进度弹窗）。
        </div>
      </div>
    </div>
  </div>`,
};

/* ── 导出页 ────────────────────────────────────────────── */
const ExportPage = {
  computed: {
    book: () => state.currentBook,
  },
  methods: {
    doExport() {
      window.open(`/api/books/${encodeURIComponent(this.book.dir_name)}/export`, '_blank');
    },
  },
  template: `
  <div>
    <div v-if="!book" class="empty"><div class="big">📤</div>请先从书架选择一本书</div>
    <div v-else style="max-width:560px">
      <div class="shelf-head"><h2>📤 导出 · 《{{ book.title }}》</h2></div>
      <div class="panel">
        <p style="line-height:2;color:#555;font-size:13px">
          将整本书导出为 Markdown 手稿（manuscript 格式）：<br>
          · 共 <b>{{ book.chapters_written }}</b> 章 · <b>{{ book.total_words }}</b> 字<br>
          · 含书名、简介、全部章节正文
        </p>
        <div class="overlay-actions" style="justify-content:flex-start">
          <button class="btn primary" @click="doExport">⬇ 下载 manuscript.md</button>
        </div>
      </div>
    </div>
  </div>`,
};

const PAGES = {
  shelf: ShelfPage,
  workbench: WorkbenchPage,
  'settings-page': SettingsPage,
  hooks: HooksPage,
  report: ReportPage,
  export: ExportPage,
};

/* ═══════════════════════════════════════════════════════
   根应用
   ═══════════════════════════════════════════════════════ */
createApp({
  setup() {
    const pageComponent = computed(() => PAGES[state.page] || ShelfPage);
    const taskSteps = computed(() => {
      if (!state.activeTask) return [];
      return state.activeTask.kind === 'new_book' ? NEW_BOOK_STEPS : WRITE_STEPS;
    });
    const stepIndex = computed(() => {
      if (!state.activeTask) return -1;
      if (state.activeTask.status === 'done') return taskSteps.value.length;
      const i = taskSteps.value.findIndex(s => s.key === state.taskStage);
      return i < 0 ? 0 : i;
    });

    function logClass(line) {
      if (line.startsWith('⚠')) return 'l-warn';
      if (line.startsWith('✗')) return 'l-err';
      if (line.startsWith('✅') || line.startsWith('✓')) return 'l-ok';
      return '';
    }

    onMounted(async () => {
      state.ready = true;
      try { state.health = await api('/api/health'); } catch (e) { /* 保持默认 */ }
      await refreshBooks();
      // 恢复上次打开的书
      const saved = localStorage.getItem('na.currentBook');
      if (saved && state.books.some(b => b.dir_name === saved)) await loadBook(saved);
      // 补偿:页面刷新时恢复进行中的任务
      try {
        const cur = await api('/api/tasks/current');
        if (cur) {
          state.activeTask = cur;
          const d = await api(`/api/tasks/${cur.id}`);
          for (const e of d.events || []) handleEvent(e);
          state.overlayOpen = cur.status === 'running';
        }
      } catch (e) { /* ignore */ }
      connectWS();
    });

    return {
      ...toRefs(state),
      go, pageComponent, taskSteps, stepIndex, logClass, writeNextChapter,
    };
  },
}).mount('#app');
