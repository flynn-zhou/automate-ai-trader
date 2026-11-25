#!/usr/bin/env node
/**
 * 演化面板的前端逻辑检查：在 Node 里跑**真的** `_evo_js.html`，喂真接口返回值。
 *
 * 用法：
 *     node tools/check_evo_ui.js <payload.json> [模板路径]
 *
 * payload.json = {meta: {...}, evo: {...}, snap: {...}}
 *   —— 三个端点的原始返回，由 tests/test_web_evo_ui_runtime.py 现取现喂。
 *
 * ## 为什么要有这个工具
 *
 * Python 测不到浏览器里的 ECharts，而这一层最可能的失败恰恰是运行时的：
 * 白屏、点了没反应、副图和主图错位。这些在 Python 里一行都不会红。
 *
 * 这里用一个最小的 document / echarts / fetch 替身，把模板里的 JS **原样**跑一遍。
 * 替身只做两件事：记录 setOption 的参数、把 click 回调留给脚本手动触发。
 * 所以它验的是「模板里的逻辑」，不是「ECharts 画得好不好看」——
 * 好看与否得靠人看，逻辑对不对不该靠人看。
 *
 * ## 它守的几条红线
 *
 * 1. 结论色带的颜色只许来自 7 个结论词的调色板。
 *    一旦有人把 status（存活结局，**事后**信息）画进色带，
 *    这个回看页面本身就变成了前视入口。
 * 2. 主图与结论带的类别必须逐根一致，否则副图整体错位 —— 不会报错，只会画歪。
 * 3. tooltip 必须有 hideDelay（这次修的一个 bug）。
 * 4. 快照面板必须同时有「当时能看到的」和「事后才知道的」两块。
 */
'use strict';

const fs = require('fs');
const path = require('path');

const payloadPath = process.argv[2];
if (!payloadPath) {
  console.error('用法: node tools/check_evo_ui.js <payload.json> [模板路径]');
  process.exit(2);
}
const tplPath = process.argv[3] ||
  path.join(__dirname, '..', 'ai_trader', 'web', 'templates', '_evo_js.html');

const PAYLOAD = JSON.parse(fs.readFileSync(payloadPath, 'utf8'));
const raw = fs.readFileSync(tplPath, 'utf8');

// ---- 从模板里把 JS 取出来，并替换掉 Jinja -------------------------
const m = raw.match(/<script>([\s\S]*?)<\/script>/);
if (!m) {
  console.error('❌ 模板里找不到 <script> 块');
  process.exit(1);
}
let src = m[1];
src = src.replace(/\{#[\s\S]*?#\}/g, '');
src = src.replace(/\{\{[\s\S]*?url_for\('kbar\.(\w+)'\)[\s\S]*?\}\}/g, '"$1"');
if (/\{\{|\{%/.test(src)) {
  console.error('❌ 还有没替换掉的 Jinja：' + src.match(/\{\{[\s\S]*?\}\}|\{%[\s\S]*?%\}/g).slice(0, 3));
  process.exit(1);
}

// ---- 最小替身 -------------------------------------------------------
const els = {};
function mkEl(id) {
  if (!els[id]) {
    els[id] = {
      id: id, style: {}, textContent: '', innerHTML: '', parentNode: null,
      classList: { add() {}, remove() {}, toggle() {} },
      // 元素级的 querySelectorAll 也必须给：替身原先只有 document 上有，
      // 而模板里用 box.querySelectorAll 给 chip 绑点击 —— 缺了它，
      // 整条 render() 会抛异常并被 load() 的 .catch 吞成一行状态文字，
      // 表现是"图根本没渲染"，排查方向完全被带偏。
      // 返回空数组是诚实的：替身不会解析 innerHTML，chip 本来就不存在。
      querySelectorAll: function () { return []; },
      getAttribute: function () { return null; },
      setAttribute: function () {},
      appendChild: function () {}
    };
  }
  return els[id];
}

let lastOption = null, clickHandler = null, initCount = 0;

const echartsStub = {
  init: function () {
    initCount++;
    return {
      on: function (ev, fn) { if (ev === 'click') clickHandler = fn; },
      setOption: function (o) { lastOption = o; },
      getOption: function () { return lastOption || { dataZoom: [{ start: 0, end: 100 }] }; },
      resize: function () {},
      clear: function () { lastOption = null; }
    };
  }
};

global.document = {
  getElementById: mkEl,
  querySelectorAll: function () { return []; }
};
global.window = {
  addEventListener: function () {},
  echarts: echartsStub          // ⚠ 模板判的是 window.echarts
};
global.echarts = echartsStub;

global.fetch = function (url) {
  let body;
  if (String(url).indexOf('snapshot') >= 0) body = PAYLOAD.snap;
  else if (String(url).indexOf('evo') >= 0) body = PAYLOAD.evo;
  else body = PAYLOAD.meta;
  return Promise.resolve({ json: function () { return Promise.resolve(body); } });
};

// ---- 跑模板 JS ------------------------------------------------------
const failures = [];
function check(cond, msg) { if (!cond) failures.push(msg); }

try {
  // eslint-disable-next-line no-eval
  eval(src);
} catch (e) {
  console.error('❌ 模板 JS 加载就抛了：' + (e && e.stack || e));
  process.exit(1);
}

const sleep = (ms) => new Promise(function (r) { setTimeout(r, ms); });

(async function () {
  if (!global.window.EvoPane) {
    console.error('❌ 模板没有暴露 window.EvoPane');
    process.exit(1);
  }

  global.window.EvoPane.show();
  await sleep(80);
  await sleep(80);

  check(lastOption, 'show() 之后没有 setOption —— 图根本没渲染');
  if (!lastOption) {
    // 渲染失败时 render() 抛的异常会被 load() 的 .catch 吞成一行状态文字，
    // 不把它打出来就只剩"没渲染"这种没法排查的信息。
    console.error('   ↳ evo-status: ' +
                  (mkEl('evo-status').textContent || '(空)'));
  }
  if (lastOption) {
    const o = lastOption;
    check(o.grid && o.grid.length === 2, 'grid 不是 2 个（主图 + 结论带）');
    check(o.xAxis && o.xAxis.length === 2, 'xAxis 不是 2 个');
    if (o.xAxis && o.xAxis.length === 2) {
      check(JSON.stringify(o.xAxis[0].data) === JSON.stringify(o.xAxis[1].data),
            '主图与结论带的类别不一致 —— 副图会整体错位');
    }
    check(o.dataZoom && o.dataZoom.length === 2, 'dataZoom 不是 2 个');
    if (o.dataZoom && o.dataZoom[0]) {
      check(JSON.stringify(o.dataZoom[0].xAxisIndex) === '[0,1]',
            'dataZoom 没同时驱动主图和结论带');
    }
    const names = (o.series || []).map(function (s) { return s.name; });
    check(names[0] === 'K线', '第一个 series 不是 K 线：' + names);
    check(names[1] === '结论', '第二个 series 不是结论带：' + names);
    check((o.series[0].data || []).length === PAYLOAD.evo.bars.length,
          'K 线根数与接口不一致');

    // 红线：色带只许用 7 词调色板
    const palette = ['#3a4250', '#5b6678', '#e8b44a', '#22c55e',
                     '#38bdf8', '#a78bfa', '#ef4444'];
    const bad = (o.series[1].data || []).filter(function (d) {
      return palette.indexOf(d.itemStyle.color) < 0;
    });
    check(bad.length === 0,
          '结论带出现调色板外的颜色 ' + bad.length + ' 个 —— 别把事后信息画进色带');

    check(o.tooltip && o.tooltip.hideDelay != null, 'tooltip 没有 hideDelay');
    check(o.tooltip && o.tooltip.hideDelay >= 300,
          'tooltip.hideDelay 太小：' + (o.tooltip && o.tooltip.hideDelay));
  }

  // 点一根 → 快照
  check(clickHandler, '没有注册 click 回调，点 K 线不会有反应');
  if (clickHandler && PAYLOAD.evo.bars.length) {
    clickHandler({ dataIndex: Math.floor(PAYLOAD.evo.bars.length / 2) });
    await sleep(100);
    const body = mkEl('snap-body').innerHTML;
    check(body && body.length > 200, '快照面板是空的（' + (body || '').length + ' 字符）');
    check(body.indexOf('当时能看到的') >= 0, '快照面板缺「当时能看到的」分栏');
    check(body.indexOf('事后才知道的') >= 0, '快照面板缺「事后才知道的」分栏');
    const lbl = mkEl('snap-label').textContent;
    check(lbl === PAYLOAD.snap.then.shape.label,
          '结论词显示不对：' + lbl + ' vs ' + PAYLOAD.snap.then.shape.label);

    const nStar = (PAYLOAD.snap.dim_catalog || []).filter(function (d) {
      return d.starred;
    }).length;
    check(nStar === 8, 'dim_catalog 里 starred 不是 8 个：' + nStar);

    const verdicts = ((PAYLOAD.snap.post || {}).preds || [])
      .map(function (p) { return p.verdict; });
    if (verdicts.length) {
      check(body.indexOf(verdicts[0]) >= 0, '事后块没有渲染判决词');
    }

    // ---- 结论筛选（点图例里的「插针」）----
    // 这段用真实数据跑 focusMask 的算法本体，不模拟 DOM 点击：
    // chip 是 innerHTML 生成的，替身没有 HTML 解析器，硬去点只能测到替身自己。
    const mask = global.window.EvoPane._focusMask;
    check(typeof mask === 'function', 'EvoPane 没有暴露 _focusMask');

    if (typeof mask === 'function') {
      const labels = PAYLOAD.evo.label;
      const ctx = global.window.EvoPane._debug().context;
      check(ctx >= 1, 'CONTEXT 不合理：' + ctx);

      // 不筛选 → null（全亮）
      check(mask(labels, null, ctx) === null, '没选结论词时不该返回掩码');

      // 出现次数最多的那个词
      const freq = {};
      labels.forEach(function (l) { if (l) freq[l] = (freq[l] || 0) + 1; });
      const top = Object.keys(freq).sort(function (a, b) {
        return freq[b] - freq[a];
      })[0];
      check(!!top, 'payload 里一个结论词都没有，测不了筛选');

      const m = mask(labels, top, ctx);
      check(Array.isArray(m) && m.length === labels.length,
            '掩码长度应等于根数');
      if (Array.isArray(m)) {
        const nOn = m.filter(Boolean).length;
        check(nOn >= freq[top], '亮的根数少于该结论本身的根数');

        // 每一条规则都要真验：该亮的亮、该暗的暗
        let badHit = 0, badMiss = 0;
        for (let i = 0; i < labels.length; i++) {
          // 是否在任意一个 top 的前后 ctx 根之内
          let should = false;
          for (let j = Math.max(0, i - ctx);
               j <= Math.min(labels.length - 1, i + ctx); j++) {
            if (labels[j] === top) { should = true; break; }
          }
          if (should && !m[i]) badHit++;
          if (!should && m[i]) badMiss++;
        }
        check(badHit === 0, badHit + ' 根该亮却没亮（上下文算漏了）');
        check(badMiss === 0, badMiss + ' 根不该亮却亮了（范围放大）');

        // 边界：第一根和最后一根不能越界
        const firstIsTop = labels[0] === top;
        if (firstIsTop && !m[0]) check(false, '第一根是目标却没亮（越界处理错）');
      }

      // 不存在的结论词 → null（不能全暗，否则看着像图坏了）
      check(mask(labels, '__不存在的结论词__', ctx) === null,
            '选了一个本周期不存在的结论词，应该当作没筛而不是全暗');
    }

    // ---- 筛选后重绘：被淡化的点必须真的带上 opacity ----
    // 直接驱动一次带筛选的渲染，确认 itemStyle 落到数据点上。
    const evo = PAYLOAD.evo;
    const freq2 = {};
    evo.label.forEach(function (l) { if (l) freq2[l] = (freq2[l] || 0) + 1; });
    const top2 = Object.keys(freq2).sort(function (a, b) {
      return freq2[b] - freq2[a];
    })[0];
    if (top2) {
      // 手写一份"筛选后的 option"不可能等于真渲染结果，替身又解析不了
      // innerHTML 去点 chip。所以这里退一步：确认模板源码里确实把 opacity
      // 写进了两处数据点的 itemStyle —— 在没有 DOM 的情况下，
      // 这是唯一能确认"接线了"的检查。真正的视觉确认要靠人眼看一次。
      // 注意用上面已经读好的 raw，不要再从 argv 取路径：
      // 测试只传 payload，模板走的是默认路径（tplPath 那条兜底）。
      check(raw.indexOf('itemStyle: { opacity: DIM }') >= 0,
            'K 线数据点没有按筛选设 opacity');
      check(raw.indexOf('st.opacity = DIM') >= 0,
            '结论色块没有按筛选设 opacity');
    }
  }

  if (failures.length) {
    console.error('❌ 演化面板前端检查失败 ' + failures.length + ' 条：');
    failures.forEach(function (f, i) { console.error('   ' + (i + 1) + '. ' + f); });
    process.exit(1);
  }
  console.log('演化面板前端检查全部通过：'
    + PAYLOAD.evo.bars.length + ' 根、series='
    + lastOption.series.map(function (s) { return s.name; }).join('/')
    + '、快照 ' + mkEl('snap-body').innerHTML.length + ' 字符');
})();
