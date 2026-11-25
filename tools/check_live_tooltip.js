#!/usr/bin/env node
/**
 * 总览图 tooltip 存活检查：模拟轮询序列，数「破坏性重建」发生了几次。
 *
 * 用法：
 *     node tools/check_live_tooltip.js <pack_payload.json>
 *
 * 背景（这个 bug 修过两次，第一次是假修）
 * ---------------------------------------
 * `_chart_js.html` 每次轮询都 `chart.setOption(opt, true)`。第二个参数
 * `notMerge=true` 会**整个替换** option，连带销毁 tooltip 实例 ——
 * 所以鼠标悬停最多活一个轮询周期（POLL_MS=3000），用户反馈"只有 2 秒"。
 *
 * 第一次修：加了一个数据签名做短路。方向对，但签名里带了
 * `bars[最后一根].close`，而实时图上最后一根是**未收盘**的那根，
 * 它的收盘价就是此刻现价，**每次轮询都在变** ⇒ 短路从不触发 ⇒
 * 等于没修。这次用行为测试把它钉住。
 *
 * 第二次修（现在）：签名只取「轴的身份」（周期 + 根数 + 最后一根时间），
 * 轴没变就只 `setOption({series})` 合并 —— 不传 tooltip/dataZoom，
 * ECharts 不会重建它们。
 *
 * 所以本工具断言的是：
 *   1. 首次渲染：notMerge=true，且带 tooltip + dataZoom（要建起来）
 *   2. 同一根 K 线内的每次 tick：**不许** notMerge，且**不许**带
 *      tooltip / dataZoom / grid（带了就会去更新它们，悬停就断）
 *   3. 来了新 K 线：允许一次 notMerge=true（一根一次，3m 就是 3 分钟一次）
 *   4. 全程「破坏性重建」次数 = 2（首帧 + 新 K 线），不是每轮一次
 */
'use strict';

const fs = require('fs');

// ⚠ 必须在覆盖 global.setInterval **之前**抓住真的那个。
//    否则后面 sleep() 会调到自己的桩，Promise 永远不 resolve，
//    整个进程静默挂住（这个坑踩过一次）。
const realSetInterval = global.setInterval;
const realSetTimeout = global.setTimeout;

const p = process.argv[2];
if (!p) {
  console.error('用法: node tools/check_live_tooltip.js <pack_payload.json>');
  process.exit(2);
}
const BASE = JSON.parse(fs.readFileSync(p, 'utf8'));
if (!BASE.ok || !BASE.live || !BASE.live.bars || !BASE.live.bars.length) {
  console.error('❌ payload 不是一份成功的 pack 返回（需要 ok + live.bars）');
  process.exit(1);
}

// ---- 由一份真实 payload 造出轮询序列 ------------------------------
function clone(o) { return JSON.parse(JSON.stringify(o)); }

// tick：轴不变（同一根未收盘 K 线），只动最后一根的 ohlc + ema 末值 + 现价
function tick(base, drift) {
  const j = clone(base);
  const b = j.live.bars[j.live.bars.length - 1];   // [t,o,c,l,h]
  b[2] = Number((b[2] + drift).toFixed(1));         // close 变
  b[3] = Math.min(b[3], b[2]);                      // low
  b[4] = Math.max(b[4], b[2]);                      // high
  j.price = b[2];
  j.change = drift;
  j.change_pct = drift / b[2] * 100;
  Object.keys(j.live.ema || {}).forEach(function (k) {
    const a = j.live.ema[k];
    if (a && a.length) a[a.length - 1] = Number((a[a.length - 1] + drift * 0.2).toFixed(1));
  });
  return j;
}

// newbar：轴变了（多一根，且最后一根时间往后一格）
function newBar(base) {
  const j = clone(base);
  const ms = j.live.interval_ms ||
             ({ '3m': 180000, '5m': 300000, '15m': 900000, '1h': 3600000 }[j.live.interval] || 180000);
  const last = j.live.bars[j.live.bars.length - 1];
  const c = Number((last[2] + 10).toFixed(1));
  j.live.bars.push([last[0] + ms, last[2], c, last[2], c]);
  Object.keys(j.live.ema || {}).forEach(function (k) {
    const a = j.live.ema[k];
    if (a && a.length) a.push(a[a.length - 1]);
  });
  j.price = c;
  return j;
}

const SEQ = [
  clone(BASE),       // 0 首帧
  tick(BASE, 3.5),   // 1 同一根在动
  tick(BASE, -2.0),  // 2
  tick(BASE, 1.5),   // 3
  newBar(BASE),      // 4 来了新 K 线
  tick(newBar(BASE), 4.0),   // 5 新一根内继续动
  tick(newBar(BASE), -1.0)   // 6
];

// ---- 替身 -----------------------------------------------------------
const els = {};
function mkEl(id) {
  if (!els[id]) {
    els[id] = { id: id, style: {}, textContent: '', innerHTML: '',
                width: 1200, height: 600,
                getContext: function () { return { clearRect() {}, drawImage() {} }; },
                clientWidth: 1200, clientHeight: 600 };
  }
  return els[id];
}
global.document = { getElementById: mkEl, querySelectorAll: function () { return []; } };
global.ChartKit = {
  fmt: function (v, d) { return (v == null || isNaN(v)) ? '--' : Number(v).toFixed(d == null ? 1 : d); },
  drawHatch: function () {},
  ema: function (a) { return a.slice(); }
};

const OPTIONS = [];          // {notMerge, hasTooltip, hasDataZoom, hasGrid, seriesIds}
const intervals = [];
let inited = 0;

global.echarts = {
  init: function () {
    inited++;
    return {
      on: function () {},
      resize: function () {},
      clear: function () {},
      getOption: function () { return { dataZoom: [{ start: 40, end: 100 }] }; },
      setOption: function (opt, notMerge) {
        OPTIONS.push({
          notMerge: !!notMerge,
          hasTooltip: Object.prototype.hasOwnProperty.call(opt, 'tooltip'),
          hasDataZoom: Object.prototype.hasOwnProperty.call(opt, 'dataZoom'),
          hasGrid: Object.prototype.hasOwnProperty.call(opt, 'grid'),
          seriesIds: (opt.series || []).map(function (s) { return s.id || s.name; })
        });
      }
    };
  }
};
global.window = {
  addEventListener: function () {},
  echarts: global.echarts
};

let fetchIdx = 0;
global.fetch = function () {
  const body = SEQ[Math.min(fetchIdx, SEQ.length - 1)];
  fetchIdx++;
  return Promise.resolve({ json: function () { return Promise.resolve(body); } });
};

// setInterval 只捕获回调，不真的排期 —— 由脚本手动驱动。
global.setInterval = function (fn, ms) { intervals.push({ fn: fn, ms: ms }); return 1; };
global.clearInterval = function () {};

// ---- 装载模板（由调用方预先渲染成纯 JS）-----------------------------
let src;
try {
  src = fs.readFileSync(process.argv[3] || '/dev/null', 'utf8');
} catch (e) {
  console.error('❌ 需要第二个参数：渲染后的 script 内容文件');
  process.exit(2);
}
try {
  eval(src);
} catch (e) {
  console.error('❌ 模板 JS 加载就抛了：' + (e && e.stack || e));
  process.exit(1);
}

const sleep = (ms) => new Promise(function (r) { realSetTimeout(r, ms); });

(async function () {
  // 超时兜底：任何一步卡住都要报出来，不能静默挂住整个测试进程。
  const guard = realSetTimeout(function () {
    console.error('❌ 检查超时（15s）—— 有 Promise 没 resolve');
    process.exit(3);
  }, 15000);
  guard.unref && guard.unref();

  await sleep(40);   // 首帧 poll（脚本末尾自己调了一次）

  // 找到 poll 那个 interval 并手动驱动
  const pollIv = intervals.filter(function (x) { return x.ms === 3000; })[0];
  const fail = [];

  if (!pollIv) fail.push('没找到 3000ms 的轮询定时器');
  if (inited !== 1) fail.push('echarts.init 被调了 ' + inited + ' 次（应该只建一次图）');
  if (!OPTIONS.length) fail.push('一次 setOption 都没发生');

  // 手动跑 6 次轮询（对应 SEQ 的 1..6）
  for (let i = 0; i < 6; i++) {
    if (pollIv) pollIv.fn();
    await sleep(30);
  }

  const destructive = OPTIONS.filter(function (o) { return o.notMerge; });

  if (OPTIONS.length < 7) {
    fail.push('setOption 只发生 ' + OPTIONS.length + ' 次，预期 ≥7（首帧 + 6 轮）');
  }
  if (destructive.length !== 2) {
    fail.push('破坏性重建（notMerge=true）发生了 ' + destructive.length
      + ' 次，预期 2（首帧 + 新 K 线各一次）。'
      + ' 每轮都重建 = tooltip 活不过一个轮询周期。');
  }
  // 首帧必须带 tooltip / dataZoom
  if (OPTIONS[0] && (!OPTIONS[0].hasTooltip || !OPTIONS[0].hasDataZoom)) {
    fail.push('首帧没有同时带 tooltip 和 dataZoom，图建不起来');
  }
  // 增量更新不许带 tooltip / dataZoom / grid
  const merges = OPTIONS.filter(function (o) { return !o.notMerge; });
  merges.forEach(function (o, i) {
    if (o.hasTooltip) fail.push('第 ' + (i + 1) + ' 次增量更新带了 tooltip —— 会重建悬停层');
    if (o.hasDataZoom) fail.push('第 ' + (i + 1) + ' 次增量更新带了 dataZoom —— 会把缩放窗口打回默认');
    if (o.hasGrid) fail.push('第 ' + (i + 1) + ' 次增量更新带了 grid');
  });
  // 增量更新必须点名 series id，否则可能按索引错配
  merges.forEach(function (o, i) {
    if (!o.seriesIds.length) fail.push('第 ' + (i + 1) + ' 次增量更新没有 series');
    if (o.seriesIds.indexOf('k') < 0) fail.push('第 ' + (i + 1) + ' 次增量更新没带 K 线 series');
  });

  if (fail.length) {
    console.error('❌ tooltip 存活检查失败 ' + fail.length + ' 条：');
    fail.forEach(function (f, i) { console.error('   ' + (i + 1) + '. ' + f); });
    process.exit(1);
  }
  console.log('tooltip 存活检查通过：共 ' + OPTIONS.length + ' 次 setOption，'
    + '其中破坏性重建 ' + destructive.length + ' 次（首帧 + 新 K 线），'
    + '其余 ' + merges.length + ' 次都是纯 series 合并。');
})();
