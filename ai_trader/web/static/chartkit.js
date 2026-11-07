/* 共用的 K 线看板逻辑。
   总览页和独立行情页都 include 这一份——
   同一张图写两遍，改一处忘一处，就会出现"两个页面表现不一样"这种问题。 */
window.ChartKit = (function () {
  "use strict";
  var HATCH_STEP = 7;

  function drawHatch(chart, el, cv, EMA_FULL) {
    if (!el || !cv || !chart) return;
    var w = el.clientWidth, h = el.clientHeight;
    if (!w || !h) return;
    var dpr = window.devicePixelRatio || 1;
    cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
    cv.style.width = w + "px"; cv.style.height = h + "px";
    var g = cv.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, h);

    var a = EMA_FULL["144"], b = EMA_FULL["169"];
    if (!a || !b) return;
    var opt = chart.getOption();
    var i144 = -1;
    for (var k = 0; k < opt.series.length; k++) {
      if (opt.series[k].name === "EMA144") i144 = k;
    }
    if (i144 < 0) return;

    var runs = [], run = null;
    for (var i = 0; i < a.length; i++) {
      var x = a[i], y = b[i];
      if (x == null || y == null) { run = null; continue; }
      // 传数据下标 i，不是时间戳——传错会静默返回 undefined，一条线都不画
      var p1 = chart.convertToPixel({ seriesIndex: i144 }, [i, x]);
      var p2 = chart.convertToPixel({ seriesIndex: i144 }, [i, y]);
      if (!p1 || !p2) { run = null; continue; }
      var up = x >= y;
      if (!run || run.up !== up) { run = { up: up, pts: [] }; runs.push(run); }
      run.pts.push([p1[0], p1[1], p2[0], p2[1]]);
    }

    runs.forEach(function (r) {
      if (r.pts.length < 2) return;
      var ps = r.pts;
      g.strokeStyle = r.up ? "rgba(34,197,94,0.55)" : "rgba(239,68,68,0.55)";
      g.lineWidth = 1;
      g.beginPath();
      var xStart = ps[0][0], xEnd = ps[ps.length - 1][0], seg = 0;
      for (var gx = Math.ceil(xStart / HATCH_STEP) * HATCH_STEP; gx <= xEnd; gx += HATCH_STEP) {
        while (seg < ps.length - 2 && ps[seg + 1][0] < gx) seg++;
        var p0 = ps[seg], p1 = ps[seg + 1] || ps[seg];
        var span = p1[0] - p0[0];
        var t = span > 0 ? (gx - p0[0]) / span : 0;
        if (t < 0) t = 0; if (t > 1) t = 1;
        var ya = p0[1] + (p1[1] - p0[1]) * t, yb = p0[3] + (p1[3] - p0[3]) * t;
        g.moveTo(gx + 0.5, Math.min(ya, yb));
        g.lineTo(gx + 0.5, Math.max(ya, yb));
      }
      var top = 1e9, bot = -1e9;
      for (var j = 0; j < ps.length; j++) {
        var tt = Math.min(ps[j][1], ps[j][3]), bb = Math.max(ps[j][1], ps[j][3]);
        if (tt < top) top = tt; if (bb > bot) bot = bb;
      }
      for (var yy = Math.ceil(top / HATCH_STEP) * HATCH_STEP; yy <= bot; yy += HATCH_STEP) {
        var runFrom = null;
        for (var j2 = 0; j2 < ps.length; j2++) {
          var t2 = Math.min(ps[j2][1], ps[j2][3]) - 0.5;
          var b2 = Math.max(ps[j2][1], ps[j2][3]) + 0.5;
          var inside = (yy >= t2 && yy <= b2);
          if (inside && runFrom === null) runFrom = ps[j2][0];
          if (!inside && runFrom !== null) {
            g.moveTo(runFrom, yy + 0.5); g.lineTo(ps[j2 - 1][0], yy + 0.5);
            runFrom = null;
          }
        }
        if (runFrom !== null) {
          g.moveTo(runFrom, yy + 0.5); g.lineTo(ps[ps.length - 1][0], yy + 0.5);
        }
      }
      g.stroke();
    });
  }

  function ema(closes, period) {
    var al = 2 / (period + 1), out = [], e = null;
    for (var i = 0; i < closes.length; i++) {
      var v = closes[i];
      e = (e === null) ? v : v * al + e * (1 - al);
      out.push(Math.round(e * 10) / 10);
    }
    return out;
  }

  function fmt(v, d) { return (v == null || isNaN(v)) ? "--" : Number(v).toFixed(d == null ? 1 : d); }

  return { drawHatch: drawHatch, ema: ema, fmt: fmt, HATCH_STEP: HATCH_STEP };
})();
