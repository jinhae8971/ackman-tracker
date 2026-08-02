/* tests/render_dashboard.js — 대시보드 렌더 스모크 테스트 (jsdom).
 *
 *   npm install jsdom && node tests/render_dashboard.js
 *
 * dashboard/build.py 를 먼저 돌려 dashboard/data/*.json 이 있어야 한다.
 * Chart.js CDN 과 fetch 는 스텁으로 대체하고, 실제 산출 JSON 을 물려
 * 사이드탭 -> 엔티티 전환 -> 비교 탭까지 한 바퀴 돌린다.
 * 콘솔 에러가 하나라도 나면 exit 1.
 */
const fs = require('fs'), path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');
const ROOT = path.join(__dirname, '..', 'dashboard');

let html = fs.readFileSync(path.join(ROOT, 'index.html'), 'utf8');
// Chart.js CDN 은 샌드박스에서 못 받으므로 스텁으로 대체한다. 렌더 로직 검증이 목적.
html = html.replace(/<script src="https:\/\/cdnjs[^"]+"><\/script>/,
  '<script>window.Chart=function(c,cfg){this.cfg=cfg;this.destroy=function(){};this.resize=function(){};' +
  'window.__charts=(window.__charts||[]);window.__charts.push(cfg);};</script>');

const vc = new VirtualConsole();
const errors = [];
vc.on('jsdomError', e => errors.push('jsdomError: ' + e.message));
vc.on('error', (...a) => errors.push('console.error: ' + a.join(' ')));

// 스텁은 페이지 스크립트가 돌기 '전에' 심어야 한다. 나중에 심으면 자동 boot() 와
// 테스트의 boot() 가 경쟁해 결과가 뒤섞인다.
const stub = (w) => {
  w.HTMLCanvasElement.prototype.getContext = () => ({});
  w.fetch = (url) => {
    const f = path.join(ROOT, String(url).replace(/^\.?\//, ''));
    if (!fs.existsSync(f)) return Promise.resolve({ ok: false, status: 404 });
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(JSON.parse(fs.readFileSync(f, 'utf8'))) });
  };
};
const dom = new JSDOM(html, { runScripts: 'dangerously', virtualConsole: vc,
                              url: 'https://x.test/', beforeParse: stub });
const w = dom.window;

(async () => {
  // 페이지가 스스로 호출한 boot() 이 끝날 때까지 기다린다.
  for (let i = 0; i < 50 && !w.document.querySelector('#entlist .ent-btn.on'); i++)
    await new Promise(r => setTimeout(r, 20));
  const $ = s => w.document.querySelector(s);
  const txt = s => ($(s) ? $(s).textContent.trim() : '<<MISSING ' + s + '>>');

  const report = [];
  report.push(['브랜드', txt('#brandname')]);
  report.push(['엔티티', txt('#entname')]);
  report.push(['사이드탭 수', w.document.querySelectorAll('#entlist .ent-btn').length]);
  report.push(['활성 탭', $('#entlist .ent-btn.on') ? $('#entlist .ent-btn.on').getAttribute('data-entity') : 'NONE']);
  report.push(['포트폴리오 행', w.document.querySelectorAll('#pf tbody tr').length]);
  report.push(['KPI 수', w.document.querySelectorAll('#kpis > *').length]);
  report.push(['이벤트 카드', w.document.querySelectorAll('#feed .ev, #feed > *').length]);
  report.push(['공시 행', w.document.querySelectorAll('#ftab tbody tr').length]);
  report.push(['배너', w.document.querySelectorAll('#banners .banner').length]);
  report.push(['차트 생성', (w.__charts || []).length]);

  // --- 엔티티 전환 ---
  for (const key of ['berkshire', 'citadel']) {
    await w.selectEntity(key);
    report.push(['--- ' + key, txt('#entname')]);
    report.push(['  포트폴리오 행', w.document.querySelectorAll('#pf tbody tr').length]);
    report.push(['  배너', [...w.document.querySelectorAll('#banners .banner')].map(b => b.textContent.slice(0, 42)).join(' | ')]);
    report.push(['  KPI 첫 라벨', txt('#kpis > *:first-child')]);
  }

  // --- 비교 탭 ---
  $('#tabs button[data-view="compare"]').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await w.ensureCompare();
  await new Promise(r => setTimeout(r, 50));
  report.push(['--- 비교 카드', w.document.querySelectorAll('#cmpCards .cmp-card').length]);
  report.push(['  공통 보유 행', w.document.querySelectorAll('#cmpCommon tbody tr').length]);
  report.push(['  엇갈림 행', w.document.querySelectorAll('#cmpOpp tbody tr').length]);
  report.push(['  비교 노트', w.document.querySelectorAll('#cmpNotes .note').length]);
  report.push(['  차트 누적', (w.__charts || []).length]);

  report.forEach(r => console.log(String(r[0]).padEnd(16), r[1]));
  console.log('\nERRORS:', errors.length ? errors.join('\n  ') : 'none');
  process.exit(errors.length ? 1 : 0);
})().catch(e => { console.error('FATAL', e); process.exit(1); });
