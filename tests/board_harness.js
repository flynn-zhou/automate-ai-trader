/* 无头渲染大屏：给 board.js 一个最小 DOM + fetch 桩，喂合成状态，
   把渲染出来的所有文字抓出来，检查有没有 undefined / NaN。
   目的：大屏的每个分支都必须"说人话"，不能把后端没给的字段印成 undefined。 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const SRC = path.join(__dirname, "..", "ai_trader", "web", "static", "board.js");
const code = fs.readFileSync(SRC, "utf8");

function makeEl(id) {
  const el = {
    id: id,
    textContent: "",
    innerHTML: "",
    className: "",
    style: {},
    clientWidth: 300,
    clientHeight: 80,
    width: 0,
    height: 0,
    classList: { add: () => {}, remove: () => {}, contains: () => false },
    getAttribute: () => "",
    setAttribute: () => {},
    appendChild: () => {},
    addEventListener: () => {},
    getContext: () => new Proxy({}, { get: () => () => {} }),
    querySelectorAll: () => [],
    getBoundingClientRect: () => ({ width: 800, height: 400, top: 0, left: 0 }),
    parentElement: null,
    onclick: null,
  };
  el.parentElement = { getBoundingClientRect: () => ({ width: 800, height: 400, top: 0, left: 0 }),
                       clientWidth: 800, clientHeight: 400 };
  return el;
}

function run(state, opts) {
  opts = opts || {};
  const els = {};
  const wsInstances = [];
  const get = (id) => (els[id] = els[id] || makeEl(id));

  const document = {
    getElementById: get,
    querySelectorAll: () => [],
    createElement: (t) => makeEl(t),
    addEventListener: () => {},
    body: makeEl("body"),
    documentElement: makeEl("html"),
  };

  const responses = {
    "/api/trade/state": state,
    "/api/trade/decisions?limit=60": { decisions: opts.decisions || [] },
    "/api/journal?limit=60": { entries: opts.journals || [] },
    "/api/trade/equity?limit=1000": { curve: opts.curve || [] },
    "/api/events?limit=40": { events: [] },
    "/api/health": { ok: true },
  };

  const sandbox = {
    console: console,
    document: document,
    location: { port: "8090", protocol: "http:", hostname: "127.0.0.1" },
    navigator: { userAgent: "node" },
    devicePixelRatio: 1,
    addEventListener: () => {},
    removeEventListener: () => {},
    matchMedia: () => ({ matches: false, addEventListener: () => {} }),
    requestAnimationFrame: () => 0,
    cancelAnimationFrame: () => {},
    setTimeout: () => 0,
    setInterval: () => 0,
    clearInterval: () => {},
    clearTimeout: () => {},
    // 页头（applyState）只由行情 WS 推送触发，普通 fetch 桩覆盖不到它。
    // 以前这里是纯空桩，于是「页头在健康时喊 WS 静默」这个缺陷
    // 一路躲过了所有测试 —— 页头逻辑此前零覆盖。
    // 现在：opts.feed 给了快照，就记录下来，等 board.js 建好连接后
    // 用 hello 消息把快照推给它（和服务器真实行为一致：board.js:548）。
    WebSocket: function () {
      this.close = () => {};
      this.send = () => {};
      this.readyState = 1;
      wsInstances.push(this);
    },
    fetch: (url) => {
      const key = Object.keys(responses).find((k) => url.indexOf(k.split("?")[0]) === 0);
      const body = key ? responses[key] : {};
      return Promise.resolve({ json: () => Promise.resolve(body), ok: true });
    },
  };
  sandbox.window = sandbox;
  sandbox.global = sandbox;
  sandbox.self = sandbox;

  vm.createContext(sandbox);
  vm.runInContext(code, sandbox, { filename: "board.js" });

  // 让 fetch 的 promise 链跑完
  return new Promise((resolve) => {
    let i = 0;
    let fed = false;
    const tick = () => {
      i++;
      // 确定性地在第 3 拍把行情快照推给页头（那时 ws 已建好、onmessage 已挂上）。
      // 不用 setTimeout：它和 setImmediate 的先后顺序不可靠。
      if (!fed && i === 3 && opts.feed) {
        fed = true;
        wsInstances.forEach(function (w) {
          if (typeof w.onmessage === "function") {
            w.onmessage({ data: JSON.stringify({ type: "hello", snapshot: opts.feed }) });
          }
        });
      }
      if (i > 30) return resolve(dump(els));
      setImmediate(tick);
    };
    tick();
  });
}

function dump(els) {
  const out = [];
  for (const id of Object.keys(els)) {
    const el = els[id];
    const t = String(el.textContent || "");
    const h = String(el.innerHTML || "");
    const c = String(el.className || "");
    // className 也必须抓出来：errorBar 就是靠 class 区分「正在坏」和「早就好了」的，
    // 只比文字的话，两种状态在断言里长得一模一样。
    if (t) out.push({ id: id, kind: "text", v: t, c: c });
    if (h) out.push({ id: id, kind: "html", v: h, c: c });
  }
  return out;
}

module.exports = { run: run };

if (require.main === module) {
  const st = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
  // node board_harness.js <state.json> [feed.json]
  // feed.json 给的是【行情源快照】，走 WS hello 推给页头；
  // state.json 给的是【交易面板】，走 fetch。两者不是一份数据。
  const opts = {};
  if (process.argv[3]) {
    opts.feed = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
  }
  run(st, opts).then((out) => {
    process.stdout.write(JSON.stringify(out, null, 1));
  });
}
