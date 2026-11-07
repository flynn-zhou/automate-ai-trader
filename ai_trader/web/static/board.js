/* 3s 实时行情 + 交易因果大屏：原生 WebSocket + Canvas，无框架依赖。
   四个面：决策因果 / 持仓·权益 / 研究日志 / 数据源。 */
(function () {
  "use strict";
  var WS_PORT = (location.port ? parseInt(location.port, 10) + 1 : 8091);
  /* 访问令牌（第 129 轮）。
     后端现在【所有 /api/* 和 WebSocket 都要令牌】，所以前端必须有携带方式，
     否则打开 AI_WEB_TOKEN 之后：每个 fetch 都 401、WS 连不上，
     而 board.js 里的 .catch(function(){}) 会把这些失败全部吞掉 ——
     用户看到一片空白，且没有任何提示。
     把令牌从地址栏 ?token=xxx 读进来，再从 localStorage 兜底，
     这样用户只需在地址里带一次。 */
  var TOKEN = (function () {
    var m = /[?&]token=([^&#]+)/.exec(location.search || "");
    if (m) {
      var v = decodeURIComponent(m[1]);
      try { localStorage.setItem("ai_token", v); } catch (e) {}
      return v;
    }
    try { return localStorage.getItem("ai_token") || ""; } catch (e) { return ""; }
  })();
  function withToken(u) {
    if (!TOKEN) return u;
    return u + (u.indexOf("?") >= 0 ? "&" : "?") + "token=" + encodeURIComponent(TOKEN);
  }
  /* WebSocket 地址（第 N 轮改成同源）。
     原来是 hostname + ":8091"，而 Caddy 只代理 443 -> 8090，
     于是通过域名访问时 wss://域名:8091 连不上，看板一路显示 "--"。
     现在：
       https（走域名/反代）-> 同源 /ws，由 Caddy 把 /ws 转到内部 8091
       http （本地直连 8090）-> 仍回落 8091，本地调试行为不变
     这样"域名能直接访问"和"本地能调试"两者都成立，不用各自记一套端口。 */
  var WS_URL = (function () {
    var scheme = location.protocol === "https:" ? "wss://" : "ws://";
    var host = location.protocol === "https:" ? location.host
                                              : location.hostname + ":" + WS_PORT;
    return scheme + host + "/ws" + (TOKEN ? "?token=" + encodeURIComponent(TOKEN) : "");
  })();

  var INTERVALS = [{ms:3000,name:"3s"},{ms:15000,name:"15s"},{ms:60000,name:"1m"},{ms:180000,name:"3m"},{ms:900000,name:"15m"}];
  var cur = 180000, MAXBARS = 400, data = {};
  INTERVALS.forEach(function(iv){ data[iv.ms] = { list: [], map: {} }; });

  var state = {}, tape = [], events = [], decisions = [], journals = [], equityCurve = [];
  var dirty = true, lastRender = 0;
  var $ = function (id) { return document.getElementById(id); };
  var cv = $("cv"), ctx = cv.getContext("2d");

  /* ---------------- 数据 ---------------- */
  function upsert(msg) {
    var st = data[msg.i]; if (!st) return;
    var b = st.map[msg.t];
    if (b) { b.o=msg.o;b.h=msg.h;b.l=msg.l;b.c=msg.c;b.v=msg.v;b.q=msg.q;b.n=msg.n;b.b=msg.b;b.s=msg.s;b.x=msg.x; }
    else {
      b = {t:msg.t,o:msg.o,h:msg.h,l:msg.l,c:msg.c,v:msg.v,q:msg.q,n:msg.n,b:msg.b,s:msg.s,x:msg.x};
      st.map[msg.t] = b;
      var i = st.list.length - 1;
      while (i >= 0 && st.list[i].t > b.t) i--;
      st.list.splice(i + 1, 0, b);
      if (st.list.length > MAXBARS) delete st.map[st.list.shift().t];
    }
    if (msg.i === 3000 && msg.x) { tape.unshift(b); if (tape.length > 60) tape.pop(); renderTape(); }
    dirty = true;
  }

  function renderTape() {
    $("tapeBody").innerHTML = tape.slice(0, 40).map(function (b) {
      var buy=b.b||0, sell=b.s||0, tot=buy+sell, ratio = tot>0?buy/tot:0.5;
      var cls = b.c >= b.o ? "up" : "down", dp = b.o ? (b.c-b.o)/b.o*100 : 0;
      return '<tr class="'+cls+'"><td>'+fmtTime(b.t)+"</td><td>"+b.c.toFixed(1)+
        "</td><td>"+(dp>=0?"+":"")+dp.toFixed(3)+"%</td><td>"+(b.n||0)+
        "</td><td>"+buy.toFixed(2)+"</td><td>"+sell.toFixed(2)+
        '</td><td style="width:46px"><div class="bar"><i style="width:'+(ratio*100).toFixed(0)+
        "%;background:"+(ratio>=0.5?"var(--up)":"var(--down)")+'"></i></div></td></tr>';
    }).join("");
  }

  function renderEvents() {
    $("events").innerHTML = events.slice(0, 30).map(function (e) {
      return '<div class="ev '+(e.level||"info")+'">'+fmtTime(e.ts)+" · "+(e.source||"")+
        " · "+(e.event||"")+" "+(e.detail||"")+"</div>";
    }).join("");
  }

  var ACT_LABEL = { open_long:"开多", open_short:"开空", close:"平仓", hold:"持有", skip:"未做", watch:"盯盘中" };

  function renderDecisions() {
    $("decList").innerHTML = decisions.slice(0, 60).map(function (d) {
      var g = (d.gates || []).map(function (x) {
        return '<span class="g '+(x.ok?"ok":"no")+'" title="'+esc(x.detail||"")+'">'+esc(x.gate)+"</span>";
      }).join("");
      var risk = d.risk || {};
      var riskTxt = risk.qty ? (" qty "+risk.qty+" · 名义 "+(risk.notional||0).toFixed(1)+
        "U · "+(risk.leverage||0).toFixed(1)+"x · 止损 "+(risk.stop_pct||0).toFixed(2)+"%") : "";
      return '<div class="dec '+d.action+'"><div class="t">'+fmtTime(d.ts)+
        ' · 价 '+Number(d.price||0).toFixed(1)+' · 权益 '+Number(d.equity||0).toFixed(2)+'U</div>'+
        '<div><span class="act">'+(ACT_LABEL[d.action]||d.action)+'</span>'+
        (d.signal?'<span class="sig">'+esc(d.signal)+'</span>':'')+
        '<span class="rs">'+esc(d.reason||"")+riskTxt+'</span></div>'+
        (g?'<div class="gates">'+g+'</div>':'')+'</div>';
    }).join("");
  }

  function renderJournals() {
    $("jrList").innerHTML = journals.slice(0, 60).map(function (j) {
      return '<div class="jr"><div><span class="k '+j.kind+'">'+esc(j.kind)+'</span>'+
        '<span class="ti">'+esc(j.title)+'</span> <span class="t">'+fmtTime(j.ts)+'</span></div>'+
        '<div class="bd">'+esc(j.body||"")+'</div></div>';
    }).join("");
  }

  function renderPosition(st) {
    var p = st.pos;
    var box = $("posCard");
    if (!p) {
      box.className = "card";
      box.innerHTML = '<h4>当前持仓</h4><div class="kv"><span>空仓</span><b>等待信号</b></div>'+
        (st.risk ? '<div class="kv"><span>今日已实现</span><b class="'+(st.risk.day_pnl>=0?'pnl-up':'pnl-down')+
        '">'+Number(st.risk.day_pnl||0).toFixed(3)+'U</b></div>'+
        '<div class="kv"><span>连续亏损</span><b>'+(st.risk.consecutive_losses||0)+' 次</b></div>':'');
      $("posChip").textContent = "空仓";
      $("posChip").style.color = "var(--dim)";
      return;
    }
    var dir = p.side === "long" ? 1 : -1;
    // 价格优先用【REST 这条消息自己带的】价格。
    // 以前用的是 state.price（WS 快照喂的），而这张卡片其余字段都来自 REST：
    // WS 一降级，state.price 就是 0 或陈旧值，浮盈会被算成一个荒谬的数
    // （实测过：开仓 78000、现价 82000，本该 +4.8U，却显示 -93.6U）。
    // 而 WS 降级恰恰是最需要看清真实盈亏的时候。
    var px = Number((st.trend && st.trend.price) || state.price || 0);
    var unreal = px > 0 ? (px - p.entry) * p.qty * dir : 0;
    box.className = "card " + (p.side === "long" ? "pos-long" : "pos-short");
    box.innerHTML = '<h4>当前持仓 · '+(p.side==="long"?"做多":"做空")+'</h4>'+
      kv("开仓价", Number(p.entry).toFixed(1)) + kv("数量", round3(p.qty)+" BTC") +
      kv("名义", (p.qty*p.entry).toFixed(1)+"U") + kv("止损", Number(p.stop).toFixed(1)) +
      // 目标：趋势策略没有固定止盈（计划里 target=0），后端也不下发这个字段。
      // 直接 Number(undefined) 会印出 NaN —— 而 NaN 会被当成一个数字读。
      kv("目标", (p.target > 0) ? Number(p.target).toFixed(1)
                                : '<span class="dim">无（跌破均线出场）</span>') +
      kv("浮动盈亏", px > 0
        ? '<span class="'+(unreal>=0?"pnl-up":"pnl-down")+'">'+unreal.toFixed(3)+"U</span>"
        : '<span class="dim">价格未知（数据源异常）</span>') +
      // 已持有：后端 pos 里没有 bars 字段，给的是 entry_ts。
      // 趋势持仓的计量单位是小时（t.hold_h），不是 3s 根数——那是剥头皮时代的写法。
      kv("已持有", (function(){
        var th = st.trend || {};
        if (typeof p.bars === "number") return p.bars+" 根 3s";
        if (typeof th.hold_h === "number") return th.hold_h.toFixed(1)+" 小时";
        if (p.entry_ts) return ((Date.now()-p.entry_ts)/3600000).toFixed(1)+" 小时";
        return "—";
      })()) +
      (p.open_reason?'<div class="kv" style="margin-top:6px"><span style="line-height:1.5">'+esc(p.open_reason)+'</span></div>':'');
    $("posChip").innerHTML = (p.side==="long"?"多":"空")+" "+round3(p.qty)+" @ "+Number(p.entry).toFixed(0)+
      ' <b class="'+(unreal>=0?"pnl-up":"pnl-down")+'">'+unreal.toFixed(2)+"U</b>";
    $("posChip").style.color = p.side==="long" ? "var(--up)" : "var(--down)";
  }

  function kv(k, v) { return '<div class="kv"><span>'+k+'</span><b>'+v+'</b></div>'; }
  function round3(v) { return (Math.round(Number(v)*1000)/1000).toFixed(3); }

  function fmt(v, n) {
    if (v === null || v === undefined || isNaN(v)) return "--";
    return Number(v).toFixed(n);
  }

  // 交易因果链：把"数据 -> 判断 -> 动作 -> 触发条件"整条链画出来。
  // 用户的原话是"我要知道你在做什么"，所以这里不只要结论，还要给判据。
  function renderTrend(st) {
    var el = document.getElementById("causalChain");
    if (!el) return;
    var t = (st && st.trend) || {};
    if (t.mode !== "trend") {
      el.innerHTML = '<span class="cStep dim">当前策略：' + (t.mode || "scalp") + '（3s 剥头皮对照）</span>';
      return;
    }
    if (!t.ready) {
      el.innerHTML = '<span class="cStep dim">趋势策略预热中：4h K线 ' +
        (t.bars || 0) + '/' + (t.lookback || 200) + '</span>';
      return;
    }
    var steps = [];
    steps.push({ cls: "", html: '4h K线 <b>' + t.bars + '</b> 根' });
    steps.push({ cls: "", html: 'SMA' + t.lookback + ' <b>' + fmt(t.sma, 1) + '</b>' });
    var dc = t.dist_pct >= 0 ? "ok" : "stop";
    steps.push({ cls: dc, html: '现价 <b>' + fmt(t.price, 1) + '</b>（' +
      (t.dist_pct >= 0 ? '+' : '') + fmt(t.dist_pct, 2) + '%）' });
    // 这一句说的是【行情状态】，不是持仓状态，所以按裸均线讲是对的。
    // 但加了滞回带之后多出一种状态：价在均线上方、却还没到触发线。
    // 只写"上升趋势"会让人以为系统该在场了，所以把这种中间态点出来。
    var regime = t.above
      ? '<b>上升趋势</b>（价在均线上方）'
      : '<b>下跌趋势</b>（价在均线下方）';
    if (t.above && t.hyst_pct > 0 && t.need_pct > 0) {
      regime += '<span class="dim">，但仍在滞回带内（未达触发线，暂不进场）</span>';
    }
    steps.push({ cls: t.above ? "ok" : "stop", html: regime });
    // 安全闸：必须画进因果链。
    //
    // 没有这一块时，大屏写「动作：空仓等待 / 开多触发线：涨到 X 并收盘确认」，
    // 暗示「价格到了就会开」。但六道闸任何一道拦住，实际都不会开
    // （数据源、峰值回撤、连亏冷却、当日盈亏超限等）。
    // 用户的原话是「我要知道你在做什么」——
    // 漏掉「为什么不能开」的因果链不是因果，只是愿望。
    // 执行器熔断：比安全闸更彻底的一层。
    // 熔断后所有下单都返回「已熔断」，系统停止交易，
    // 但表现是「一直空仓」——和大屏上「空仓等待 / 涨到 X 就开」完全一样。
    // 不画出来，用户会以为在等信号，其实永远等不到。
    var ex = st.executor || {};
    if (ex.halted) {
      steps.push({ cls: "stop act", html: '<b>执行器已熔断：完全停止交易</b>' });
      steps.push({ cls: "stop", html: (ex.reason || "原因未提供") +
        '<span class="dim">（需人工排查后重启）</span>' });
    }

    var g = st.guard || {};
    // 注意：blocked 里包含了执行器熔断，但【安全闸那一步只报闸自己的状态】。
    // 否则熔断时会打出「安全闸拦截：原因未提供」——
    // 明明闸是放行的，拦住的是执行器，这两层必须分清。
    var guardBlocked = (g.can_open === false);
    var blocked = guardBlocked || !!ex.halted;
    // 硬停和临时闸必须分开画（第 118 轮）。
    //
    // 以前这里只有一句 (g.block_reason || "原因未提供")，所以
    // halted_hard / halt_reason 后端算得再准，屏幕上也一个字都没有。
    // 而两者的处置方式完全不同：
    //   临时闸 -> 等它自己恢复（数据源回来、当日翻篇、平仓成功）
    //   硬停   -> 【必须人工复位】，不会自己好
    // 混成一句，用户会一直等一个永远不会自己恢复的状态。
    var hardHalt = !!g.halted_hard;
    if (hardHalt) {
      // 硬停有两种，处置方式完全相反（第 122 轮）：
      //
      //   drawdown 回撤熔断 -> 冷却期到了自己解除，什么都不用做
      //   mismatch 账实不符 -> 永远不会自己解除，必须立刻查账
      //
      // 以前一律写「且不会自动恢复」。加了冷却期之后那句话就是错的：
      // 用户会跑去手工复位一个本来自己会好的状态。
      // 反过来更糟——如果账实不符也显示"还有 N 天自动解除"，
      // 用户会以为等着就好，而钱可能已经放错了地方。
      var isDrawdown = String(g.halt_kind || "") === "drawdown";
      var left = g.release_in_days;
      if (isDrawdown && left !== null && left !== undefined) {
        steps.push({ cls: "stop act", html: '<b>硬停：已停止开仓（回撤熔断）</b>' });
        steps.push({ cls: "stop", html: (g.halt_reason || g.block_reason || "原因未提供") +
          '<span class="dim">（还有 <b>' + fmt(left, 1) + '</b> 天自动解除，不用人工干预）</span>' });
      } else if (isDrawdown) {
        // 冷却期被关掉（hard_halt_cooldown_days<=0）时的兜底：
        // 仍然要说清楚"不会自己好"，否则用户会一直等。
        steps.push({ cls: "stop act", html: '<b>硬停：已停止开仓，且不会自动恢复</b>' });
        steps.push({ cls: "stop", html: (g.halt_reason || g.block_reason || "原因未提供") +
          '<span class="dim">（需人工复位 /api/guard/reset）</span>' });
      } else {
        steps.push({ cls: "stop act", html: '<b>硬停：已停止开仓，且不会自动恢复</b>' });
        steps.push({ cls: "stop", html: (g.halt_reason || g.block_reason || "原因未提供") +
          '<span class="dim">（这条闸必须人工核对后复位 /api/guard/reset，等不会好）</span>' });
      }
    } else if (guardBlocked) {
      steps.push({ cls: "stop act", html: '<b>安全闸拦截：不会开仓</b>' });
      // 优先显示 temp_block：它是第 117 轮拆格后的合成值，
      // 优先级由 Guard.TEMP_ORDER 定（止损失效 > 引擎卡住 > 日亏损 > 数据源）。
      // 前端【原样显示】，不自己拼——前后端各拼一套就会出现两个说法。
      steps.push({ cls: "stop", html: (g.temp_block || g.block_reason || "原因未提供") +
        '<span class="dim">（临时限制，恢复后自动解除）</span>' });
    } else if (g.can_open === null || g.can_open === undefined) {
      // 未知不能显示成「可以开」。把"不知道"说成"没问题"是最糟的一种谎。
      steps.push({ cls: "dim", html: '安全闸：未接入（开仓是否放行未知）' });
    } else {
      var ddt = (g.drawdown_pct === null || g.drawdown_pct === undefined)
        ? "" : ('，回撤 ' + fmt(g.drawdown_pct, 1) + '%');
      // 「回撤熔断线」不能写成「硬停线」（第 118 轮真跑 JS 才发现的）。
      // 这行本来是「硬停线 60%」，和下面真正的「硬停：已停止开仓」用了
      // 同样两个字 —— 一个说的是【阈值】，一个说的是【状态】。
      // 我自己的测试就先被它误伤了一次，那用户更会混。
      steps.push({ cls: "ok", html: '安全闸放行' + ddt +
        '（回撤熔断线 ' + fmt((g.rules || {}).max_drawdown_pct, 0) + '%）' });
    }

    // 止损失效（第 116 轮的后端字段，第 118 轮才真的画出来）。
    //
    // 只在 >0 时显示：正常运行时挂一句「平仓失败 0 次」是噪音，
    // 噪音会让人学会无视这一整块，等于没有这块。
    // 显示的是【连续】失败次数，不是累计——连续才有意义，
    // 累计次数永远只增不减，看久了就是个无意义的数字。
    var cf = Number((ex || {}).close_fail_count || 0);
    if (cf > 0) {
      steps.push({ cls: "stop act", html: '<b>止损通道异常：平仓连续失败 <b>' +
        cf + '</b> 次</b>' });
      steps.push({ cls: "stop", html:
        '仓位保护不住（卖出指令打不出去），已禁止开新仓' +
        '<span class="dim">（平仓仍在重试）</span>' });
    }

    if (t.hold && t.pos) {
      steps.push({ cls: "act ok", html: '持仓：多 <b>' + fmt(t.pos.qty, 4) + '</b> @ ' +
        fmt(t.pos.entry, 1) + '（' + (t.pos_pnl_pct >= 0 ? '+' : '') +
        fmt(t.pos_pnl_pct, 2) + '%）' });
      steps.push({ cls: "", html: '熔断止损 <b>' + fmt(t.pos.stop, 1) + '</b>' });
      steps.push({ cls: "", html: '已持有 <b>' + fmt(t.hold_h, 1) + '</b> 小时' });
      steps.push({ cls: "ok", html: '出场条件：4h 收盘跌破 SMA' + t.lookback });
    } else {
      // 措辞要区分"闸拦住"和"执行器熔断"：两者都导致不开，
      // 但一个是策略级的临时限制，一个是需要人工介入的停机。
      var why = guardBlocked ? '安全闸未放行' : (ex.halted ? '执行器已熔断' : '');
      steps.push({ cls: blocked ? "act stop" : "act warn",
        html: blocked ? ('<b>动作：空仓等待（' + why + '，即使触发也不开）</b>')
                      : '<b>动作：空仓等待</b>' });
      // "还需涨 X%" 只在均线静止时才成立。SMA200 每根 4h 都在滚动，
      // 价格不动它自己也会下来——实测当前状态下 31 天后自然上穿。
      // 只报静止门槛会把"在等均线下来"说成"在等价格上涨"，两者的预期完全不同。
      // 第六十六轮为了纠正"把均线说成静止门槛"，加上了"均线自己下来"这句。
      // 但加了滞回带之后，这句话在数学上【不成立】：
      // 价格不动的话 SMA 最终收敛到现价本身，而进场要现价 > SMA*(1+h)，
      // SMA 最多等于现价，永远够不到现价*(1+h)。
      // 所以那种情况下必须说"等不到"，不能沉默地少一句话——
      // 沉默会让用户以为系统在正常等待，其实在等一件不会发生的事。
      var roll = '';
      if (t.roll_bars) {
        roll = '<span class="dim">；或价格不动、均线自己下来，约 <b>' +
          fmt(t.roll_days, 0) + '</b> 天后上穿</span>';
      } else if (t.roll_never) {
        roll = '<span class="dim">；若价格一直不动，均线自己下来也<b>到不了</b>' +
          '这条线（进场要求高出均线 ' + fmt(t.hyst_pct, 2) +
          '%，均线最多降到现价），所以这一笔必须靠价格自己涨</span>';
      }
      // 触发线用引擎报的 trigger，不能用裸 sma。
      // 有滞回带时两者不同：进场要收盘 > SMA*(1+h)，
      // 报裸 SMA 会让用户盯着一个到了也不开仓的价格。
      // 把滞回带一起说出来，用户才知道那 0.2% 是干什么用的。
      var trig = (t.trigger === undefined || t.trigger === null) ? t.sma : t.trigger;
      var hyst = (t.hyst_pct && t.hyst_pct > 0)
        ? '<span class="dim">（SMA ' + fmt(t.sma, 1) + ' × 1+' +
          fmt(t.hyst_pct, 2) + '%，避开擦边的假突破）</span>'
        : '';
      steps.push({ cls: "warn", html: '开多触发线：涨到 <b>' + fmt(trig, 1) +
        '</b>（还需 <b>+' + fmt(t.need_pct, 2) + '%</b>）并收盘确认' + hyst + roll +
        (blocked ? '<span class="dim">（当前' + why + '，不会执行）</span>' : '') });
      // 等待预期：把"在等"变成"等多久是正常的"。
      // 没有这个，用户无法区分「正常等待」和「系统卡住了」——
      // 而这两种的处置完全不同，大屏上却都只是"空仓等待"。
      // 必须标明是历史频率，不是预测。
      if (t.wait) {
        // 这张表按【真触发线】SMA*(1+h) 标定，不是按裸均线——
        // 否则报的是"涨回均线要多久"，比真正进场乐观（实测差 0~0.8 天）。
        // 若有人改了滞回带却没重算表，后端会打 extrapolated 标记，
        // 必须显式提示，不能安静地继续报一个不准确的数。
        var wnote = t.wait.extrapolated
          ? '<span class="dim">（表按 ' + fmt(t.wait.calibrated_hyst_pct, 2) +
            '% 标定，当前 ' + fmt(t.wait.requested_hyst_pct, 2) +
            '%，属外推，仅供参考）</span>'
          : '';
        steps.push({ cls: "dim", html: '历史同档等待 <b>' + fmt(t.wait.median_days, 1) +
          '</b> 天（中位，P90 ' + fmt(t.wait.p90_days, 1) + ' 天，样本 ' +
          t.wait.samples + '）' + wnote });
      }
      steps.push({ cls: "", html: '下一根 4h 收盘 <b>' + fmt(t.next_bar_h, 1) + '</b> 小时后' });
      // 计划不可行时长什么样（第 120 轮）。
      //
      // engine.py 第 966-970 行：计划不可行就把 notional/qty 置 0，
      // 原因塞进 plan_blocked。而这里以前只画那两个 0 ——
      // 屏幕上是「届时仓位：1.0x 名义 0U / qty 0」，
      // **为什么是 0，一个字都没有。** 而那正是用户问得最多的一句。
      //
      // 100 USDT 的账户贴着现货最小下单额走，「名义 0U」是真会出现的状态。
      if (t.plan_blocked) {
        steps.push({ cls: "stop act", html: '<b>届时不会开仓：' + esc(t.plan_blocked) + '</b>' });
      } else {
        steps.push({ cls: "", html: '届时仓位：<b>' + fmt(t.target_leverage, 1) + 'x</b> 名义 <b>' +
          fmt(t.plan_notional, 0) + 'U</b> / qty <b>' + fmt(t.plan_qty, 4) + '</b>' });
      }
    }
    // 最近一根 4h K 是哪来的（第 120 轮）。
    //
    // engine.py 第 880-883 行的注释原话：「收盘价两种口径一致，所以 SMA
    // 区分不出用的是官方还是本地；只有成交量能区分。不报出来，
    // 回退到本地 bar 在线上就完全不可见。」
    // 字段早就加进快照了，前端从来没读过 —— 那句注释要防的事，一直在发生。
    //
    // 只有「回退」才提示：官方 bar 也提示的话这就成了常驻横幅，
    // 用户会学会无视它，真回退的时候就没人看了。
    if (t.bar_src && String(t.bar_src).indexOf("local") === 0) {
      steps.push({ cls: "stop act", html: '<b>4h K 回退到本地拼接</b>' +
        '（成交量 ' + fmt(t.bar_vol_local, 0) + '，官方口径约 ' +
        fmt(t.bar_vol, 0) + '）<span class="dim">SMA 可能因此偏移</span>' });
    }
    var html = [];
    for (var i = 0; i < steps.length; i++) {
      if (i) html.push('<span class="cArrow">→</span>');
      html.push('<span class="cStep ' + steps[i].cls + '">' + steps[i].html + '</span>');
    }
    el.innerHTML = html.join("");
  }

  function renderStats(st) {
    var s = st.stats || {};
    // 统计必须标明"这是哪个策略的战绩"。
    // 这 4 笔亏损来自已废弃的剥头皮策略，而趋势策略是 0 笔；
    // 不标注的话，看大屏的人会把别人的战绩当成现在这套系统的。
    var tag = s.strategy ? ("【" + s.strategy + "】") : "";
    var html = "";
    if (s.strategy && (s.trades||0) === 0) {
      // 0 笔要明说"还没有交易"，不能显示成 0% 胜率让人以为很差
      html += kv(tag + "已平仓", "0 笔（该策略尚无成交）");
    } else {
      html += kv(tag + "已平仓笔数", (s.trades||0));
    }
    html += kv("胜率", s.win_rate==null?"—":(s.win_rate*100).toFixed(1)+"%") +
      kv("净盈亏", '<span class="'+((s.pnl_net||0)>=0?"pnl-up":"pnl-down")+'">'+
         Number(s.pnl_net||0).toFixed(3)+"U</span>") +
      kv("决策记录", (s.decisions||0));
    // 分策略分解：整体和分策略都摊开，不用汇总数掩盖组成
    var by = s.by_strategy || {};
    var keys = Object.keys(by);
    if (keys.length > 1 || (keys.length === 1 && s.strategy && keys[0] !== s.strategy)) {
      html += '<div class="kv"><span>历史分策略</span><span>' +
        keys.map(function (k) {
          var v = by[k] || {};
          return k + " " + (v.trades||0) + "笔 " +
                 (Number(v.pnl_net||0) >= 0 ? "+" : "") + Number(v.pnl_net||0).toFixed(2) + "U";
        }).join(" ｜ ") + '</span></div>';
    }
    $("statBox").innerHTML = html;
    var bs = st.block_stats || {};
    var ks = Object.keys(bs);
    $("blockBox").innerHTML = ks.length
      ? ks.map(function (k) { return kv(k, bs[k]+" 次被拒"); }).join("")
      : '<div class="kv"><span>暂无（还没有信号被风控拒绝）</span></div>';
  }

  function drawEquity() {
    var c = $("eqc"); if (!c) return;
    var w = c.clientWidth, h = c.clientHeight;
    var dpr = window.devicePixelRatio || 1;
    c.width = w*dpr; c.height = h*dpr;
    var g = c.getContext("2d"); g.setTransform(dpr,0,0,dpr,0,0);
    g.clearRect(0,0,w,h);
    if (equityCurve.length < 2) {
      g.fillStyle = "#5d6c7e"; g.font = "11px monospace";
      g.fillText("采集中…", 6, h/2); return;
    }
    var ys = equityCurve.map(function(p){ return p.equity; });
    var lo = Math.min.apply(null, ys), hi = Math.max.apply(null, ys);
    if (hi - lo < 1e-6) { hi += 0.5; lo -= 0.5; }
    var n = equityCurve.length;
    var up = ys[n-1] >= ys[0];
    g.strokeStyle = up ? "#26a69a" : "#ef5350"; g.lineWidth = 1.5;
    g.beginPath();
    equityCurve.forEach(function(p, i) {
      var x = i/(n-1)*w, y = h - (p.equity-lo)/(hi-lo)*(h-6) - 3;
      if (i===0) g.moveTo(x,y); else g.lineTo(x,y);
    });
    g.stroke();
    g.fillStyle = "#5d6c7e"; g.font = "10px monospace";
    g.fillText(hi.toFixed(2), 4, 10); g.fillText(lo.toFixed(2), 4, h-3);
  }

  function applyState(s) {
    if (!s) return;
    state = s;
    $("src").textContent = s.src || "--";
    $("srcDot").className = "dot " + (s.src||"DOWN");
    $("lastPrice").textContent = s.price ? Number(s.price).toFixed(1) : "--";
    // 以前这里固定写「WS 静默」，而它只是一直在走的空闲计时。
    // 2026-11-02 实测：源是 WS、tick 在涨、一切正常，页头照样显示「WS 静默 1.0s」——
    // 用户读到的是「行情断了」，实际什么都没断。
    // 常驻的假告警比没有告警更糟：它会训练用户无视这一格，真断的时候就没人看了。
    // 真正的告警信号是快照里的 ws_silent（feed.py:344，阈值 max(60s, ws_stale_ms)）。
    var idleMs = Number(s.ws_idle_ms || 0);
    var idleTxt = idleMs >= 999000 ? "--" : (idleMs / 1000).toFixed(1) + "s";
    var silent = !!s.ws_silent;
    $("wsChip").innerHTML = (silent ? "WS 静默 " : "WS 正常 ") + '<b id="wsIdle">' + idleTxt + "</b>";
    $("wsChip").className = "chip" + (silent ? " warn" : "");
    $("restLat").textContent = (s.rest_latency_ms||0)+"ms";
    $("ticks").textContent = s.ticks_total || 0;
    $("uptime").textContent = fmtDur(s.uptime_s||0);
    var b3 = (s.bars && s.bars[3000]) || null;
    if (b3 && b3.o) {
      var dp = (b3.c-b3.o)/b3.o*100;
      $("delta").textContent = (dp>=0?"+":"")+dp.toFixed(3)+"%";
      $("delta").className = dp>=0?"pnl-up":"pnl-down";
    }
    if (s.bars) {
      ["3000","60000","180000","900000"].forEach(function(k){
        var el = $("s"+(k==="3000"?"3":k==="60000"?"60":k==="180000"?"180":"900"));
        if (el && s.bars[k]) el.textContent = (s.bars[k].n||0)+" 笔";
      });
    }
    dirty = true;
  }

  /* ---------------- 绘制 K 线 ---------------- */
  function resize() {
    var r = cv.parentElement.getBoundingClientRect();
    var dpr = window.devicePixelRatio || 1;
    cv.width = Math.max(320, Math.floor(r.width*dpr));
    cv.height = Math.max(200, Math.floor(r.height*dpr));
    ctx.setTransform(dpr,0,0,dpr,0,0);
    dirty = true;
  }

  function draw() {
    var W = cv.clientWidth, H = cv.clientHeight;
    var PAD_R = 74, PAD_L = 6, PAD_T = 52, PAD_B = 22;
    var volH = Math.round((H-PAD_T-PAD_B)*0.17);
    var priceH = H-PAD_T-PAD_B-volH-8;
    var chartW = W-PAD_R-PAD_L;
    ctx.clearRect(0,0,W,H); ctx.fillStyle = "#0d131b"; ctx.fillRect(0,0,W,H);

    var st = data[cur], bars = st.list.slice(-MAXBARS);
    if (!bars.length) {
      ctx.fillStyle = "#768494"; ctx.font = "13px monospace";
      ctx.fillText("等待数据…", PAD_L+8, PAD_T+20); return;
    }
    var hi=-Infinity, lo=Infinity, vmax=0;
    bars.forEach(function(b){ if(b.h>hi)hi=b.h; if(b.l<lo)lo=b.l; if(b.v>vmax)vmax=b.v; });
    var pad=(hi-lo)*0.08 || hi*0.0005; hi+=pad; lo-=pad;
    var n=bars.length, slot=chartW/n, cw=Math.max(1.2, Math.min(16, slot*0.68));
    var yOf=function(p){ return PAD_T+priceH-(p-lo)/(hi-lo)*priceH; };
    var xOf=function(i){ return PAD_L+slot*(i+0.5); };

    ctx.strokeStyle="#161f2b"; ctx.fillStyle="#5d6c7e"; ctx.font="11px monospace"; ctx.lineWidth=1;
    for (var i=0;i<=6;i++) {
      var p=lo+(hi-lo)*i/6, y=yOf(p);
      ctx.beginPath(); ctx.moveTo(PAD_L,y); ctx.lineTo(PAD_L+chartW,y); ctx.stroke();
      ctx.fillText(p.toFixed(1), PAD_L+chartW+6, y+3.5);
    }
    bars.forEach(function(b,idx){
      var x=xOf(idx), up=b.c>=b.o, col=up?"#26a69a":"#ef5350";
      ctx.strokeStyle=col; ctx.fillStyle=col;
      ctx.beginPath(); ctx.moveTo(x,yOf(b.h)); ctx.lineTo(x,yOf(b.l)); ctx.stroke();
      var yo=yOf(b.o), yc=yOf(b.c), top=Math.min(yo,yc), hgt=Math.max(1,Math.abs(yc-yo));
      if (b.x===false) ctx.fillStyle = up?"rgba(38,166,154,0.72)":"rgba(239,83,80,0.72)";
      ctx.fillRect(x-cw/2, top, cw, hgt);
    });
    var volTop=PAD_T+priceH+8;
    bars.forEach(function(b,idx){
      var x=xOf(idx), hgt=vmax>0?(b.v/vmax)*(volH-6):0, up=b.c>=b.o;
      ctx.fillStyle=up?"rgba(38,166,154,0.55)":"rgba(239,83,80,0.55)";
      ctx.fillRect(x-cw/2, volTop+volH-hgt, cw, hgt);
      var tot=(b.b||0)+(b.s||0);
      if (tot>0 && cw>2) {
        ctx.fillStyle="rgba(74,163,255,0.9)";
        ctx.fillRect(x-cw/2, volTop+volH-hgt, Math.max(1,cw*((b.b||0)/tot)), 2);
      }
    });
    // 持仓线
    if (state.pos) {
      var pp = state.pos;
      ctx.setLineDash([5,4]); ctx.lineWidth=1;
      ctx.strokeStyle="#26a69a"; var ey=yOf(pp.entry);
      ctx.beginPath(); ctx.moveTo(PAD_L,ey); ctx.lineTo(PAD_L+chartW,ey); ctx.stroke();
      ctx.strokeStyle="#ef5350"; var sy=yOf(pp.stop);
      ctx.beginPath(); ctx.moveTo(PAD_L,sy); ctx.lineTo(PAD_L+chartW,sy); ctx.stroke();
      ctx.strokeStyle="#4aa3ff"; var ty=yOf(pp.target);
      ctx.beginPath(); ctx.moveTo(PAD_L,ty); ctx.lineTo(PAD_L+chartW,ty); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle="#93a2b3"; ctx.font="10px monospace";
      ctx.fillText("entry", PAD_L+2, ey-3); ctx.fillText("stop", PAD_L+2, sy-3);
      ctx.fillText("target", PAD_L+2, ty-3);
    }
    var last=bars[n-1], ly=yOf(last.c);
    ctx.strokeStyle="#4aa3ff"; ctx.setLineDash([3,3]);
    ctx.beginPath(); ctx.moveTo(PAD_L,ly); ctx.lineTo(PAD_L+chartW,ly); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle="#4aa3ff"; ctx.fillRect(PAD_L+chartW+2, ly-8, PAD_R-4, 16);
    ctx.fillStyle="#04080d"; ctx.fillText(last.c.toFixed(1), PAD_L+chartW+5, ly+4);
    ctx.fillStyle="#5d6c7e";
    var every=Math.max(1,Math.floor(n/8));
    for (var j=0;j<n;j+=every) ctx.fillText(fmtClock(bars[j].t), xOf(j)-16, H-6);

    var ivName=(INTERVALS.filter(function(x){return x.ms===cur;})[0]||{}).name||"";
    $("legend").innerHTML = "周期 <b style='color:#d7e0ea'>"+ivName+"</b> · "+n+" 根 · "+
      "<b style='color:#d7e0ea'>"+lo.toFixed(0)+" ~ "+hi.toFixed(0)+"</b><br/>"+
      "蓝条 = 主动买占比；虚线 = 当前持仓的 入场/止损/目标";
  }

  /* ---------------- 工具 ---------------- */
  function esc(s){ return String(s==null?"":s).replace(/[&<>"]/g,function(c){
    return {"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;"}[c]; }); }
  function pad2(n){ return n<10?"0"+n:""+n; }
  function fmtTime(ms){ var d=new Date(ms); return pad2(d.getHours())+":"+pad2(d.getMinutes())+":"+pad2(d.getSeconds()); }
  function fmtClock(ms){ var d=new Date(ms); return pad2(d.getHours())+":"+pad2(d.getMinutes()); }
  function fmtDur(s){ var h=Math.floor(s/3600),m=Math.floor((s%3600)/60),ss=s%60;
    return (h?h+"h":"")+(m?m+"m":"")+ss+"s"; }

  /* ---------------- 引擎报错横幅 ----------------
     引擎把错误写进 last_error / last_error_ts，但以前大屏一个字都不显示。
     三种情况必须能被区分开：
       1) 现在正出错        -> 红条，带「刚刚」
       2) 很久以前出过错    -> 淡色条，带「X 前」，不吓人
       3) 从没出过错        -> 不占位
     若把三者合成一种表现，用户就分不清"系统正在坏"和"系统好着呢"。 */
  function renderError(st) {
    var el = $("errBar");
    if (!el) return;
    var msg = (st && st.last_error) ? String(st.last_error) : "";
    if (!msg) { el.style.display = "none"; el.textContent = ""; el.className = ""; return; }
    var ts = Number((st && st.last_error_ts) || 0);
    var ageS = ts > 0 ? Math.max(0, (Date.now() - ts) / 1000) : null;
    // 10 分钟以内算"现在"：引擎主循环每 3 秒一轮，真坏了会持续刷新时间戳。
    var hot = ageS !== null && ageS < 600;
    var when;
    if (ts <= 0) { when = "时间未知"; }
    else if (hot) { when = "刚刚"; }
    else if (ageS < 3600) { when = Math.round(ageS / 60) + " 分钟前"; }
    else if (ageS < 86400) { when = Math.round(ageS / 3600) + " 小时前"; }
    else { when = Math.round(ageS / 86400) + " 天前"; }
    el.style.display = "flex";
    el.className = hot ? "" : "hist";
    el.innerHTML = '<span class="tag">' + (hot ? "引擎报错" : "历史错误") +
      "</span><span class=\"msg\">" + esc(msg) + "</span>" +
      '<span class="tag">' + when + "</span>";
  }

  /* ---------------- 轮询交易面 ---------------- */
  function pollTrade() {
    fetch(withToken("/api/trade/state")).then(function(r){return r.json();}).then(function(st){
      if (st && !st.error) {
        renderPosition(st); renderStats(st); renderTrend(st); renderError(st);
        if (st.risk) {
          $("dayPnl").textContent = Number(st.risk.day_pnl||0).toFixed(3)+"U";
          $("dayPnl").className = st.risk.day_pnl >= 0 ? "pnl-up" : "pnl-down";
        }
        // 头部权益原来只靠 WebSocket 快照喂；WS 一降级就显示 "--"，
        // 而这恰恰是最需要看清权益的时候。这里用 REST 兜底。
        if (st.equity) {
          $("equity").textContent = Number(st.equity).toFixed(2)+"U";
          $("equity").className = "pnl-up";
        }
      }
    }).catch(function(){});
    fetch(withToken("/api/trade/decisions?limit=60")).then(function(r){return r.json();}).then(function(j){
      if (j && j.decisions) { decisions = j.decisions; renderDecisions(); }
    }).catch(function(){});
    fetch(withToken("/api/journal?limit=60")).then(function(r){return r.json();}).then(function(j){
      if (j && j.entries) { journals = j.entries; renderJournals(); }
    }).catch(function(){});
    fetch(withToken("/api/trade/equity?limit=1000")).then(function(r){return r.json();}).then(function(j){
      if (j && j.curve) { equityCurve = j.curve; drawEquity(); }
    }).catch(function(){});
  }

  /* ---------------- 工具栏 & 连接 ---------------- */
  function buildToolbar() {
    var tb = $("toolbar"); tb.innerHTML = "";
    INTERVALS.forEach(function (iv) {
      var b = document.createElement("button");
      b.textContent = iv.name; if (iv.ms === cur) b.className = "on";
      b.onclick = function () {
        cur = iv.ms;
        Array.prototype.forEach.call(tb.children, function(c){ c.className = ""; });
        b.className = "on"; dirty = true;
      };
      tb.appendChild(b);
    });
  }

  function connect() {
    var ws;
    try { ws = new WebSocket(WS_URL); } catch(e) { setTimeout(connect, 2000); return; }
    $("connState").textContent = "连接中…";
    ws.onopen = function(){ $("connState").textContent = "已连接"; $("connState").style.color = "var(--ok)"; };
    ws.onmessage = function(ev) {
      var msg; try { msg = JSON.parse(ev.data); } catch(e){ return; }
      if (msg.type === "hello") applyState(msg.snapshot);
      else if (msg.type === "history") {
        var st = data[msg.interval_ms];
        if (st) {
          msg.bars.forEach(function(r){
            var b={t:r.t,o:r.o,h:r.h,l:r.l,c:r.c,v:r.v,q:r.q,n:r.n,b:r.b,s:r.s,x:true};
            if (!st.map[b.t]) { st.map[b.t]=b; st.list.push(b); }
          });
          st.list.sort(function(a,b){return a.t-b.t;});
          while (st.list.length > MAXBARS) delete st.map[st.list.shift().t];
          dirty = true;
        }
      }
      else if (msg.type === "bar") upsert(msg);
      else if (msg.type === "state") applyState(msg);
      else if (msg.type === "decision") {
        decisions.unshift({ts:msg.ts, action:msg.action, signal:msg.signal, reason:msg.reason,
          price:msg.price, equity:msg.equity, risk:msg.risk, gates:[]});
        if (decisions.length > 200) decisions.pop();
        renderDecisions();
        fetch(withToken("/api/trade/equity?limit=1000")).then(function(r){return r.json();})
          .then(function(j){ if (j && j.curve) { equityCurve = j.curve; drawEquity(); } }).catch(function(){});
      }
      else if (msg.type === "feed_event") {
        events.unshift(msg); if (events.length > 60) events.pop(); renderEvents();
      }
    };
    ws.onclose = function(){
      $("connState").textContent = "断开，2s 后重连…"; $("connState").style.color = "var(--err)";
      setTimeout(connect, 2000);
    };
    ws.onerror = function(){ try { ws.close(); } catch(e){} };
  }

  function setupTabs() {
    var tabs = document.querySelectorAll(".tabs button");
    Array.prototype.forEach.call(tabs, function (b) {
      b.onclick = function () {
        Array.prototype.forEach.call(tabs, function(x){ x.classList.remove("on"); });
        b.classList.add("on");
        Array.prototype.forEach.call(document.querySelectorAll(".pane"), function(p){ p.classList.remove("on"); });
        $(b.getAttribute("data-pane")).classList.add("on");
        if (b.getAttribute("data-pane") === "pane-pos") setTimeout(drawEquity, 30);
      };
    });
  }

  function loop(ts) {
    if (dirty && ts - lastRender > 90) { lastRender = ts; dirty = false; draw(); }
    requestAnimationFrame(loop);
  }

  window.addEventListener("resize", function(){ resize(); drawEquity(); });
  buildToolbar(); setupTabs(); resize(); renderEvents();
  pollTrade(); setInterval(pollTrade, 2500);
  connect();
  requestAnimationFrame(loop);

  fetch(withToken("/api/events?limit=40")).then(function(r){return r.json();}).then(function(j){
    if (j && j.events) { events = j.events.map(function(e){
      return {level:e.level,source:e.source,event:e.event,detail:e.detail,ts:e.ts}; }); renderEvents(); }
  }).catch(function(){});
})();
