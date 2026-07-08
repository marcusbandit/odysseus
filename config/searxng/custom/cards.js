/* cards.js: client-side interactivity for the caelestia hero answer cards.
 *
 * Enhances two server-rendered instant-answer cards:
 *   [data-cael-stock]    price history chart, range pills, live change chip
 *   [data-cael-currency] converter inputs, swap button, rate history chart
 *
 * Vanilla JS, no libraries. Loaded with <script defer> from base.html and
 * styled by cards/interactive.css. Every card is optional on any page:
 * bail silently when roots or sub-elements are missing, never log, never
 * throw on pages with no cards. Multiple cards per page are supported
 * (all state lives in per-card closures).
 */
(function () {
  "use strict";

  var SVG_NS = "http://www.w3.org/2000/svg";
  var VB_W = 560;
  var VB_H = 150;
  var DAY = 86400;

  var STOCK_RANGES = [
    { label: "1M", days: 30 },
    { label: "3M", days: 91 },
    { label: "6M", days: 182 },
    { label: "1Y", days: 365 }
  ];
  var CURRENCY_RANGES = [
    { label: "1M", days: 30 },
    { label: "3M", days: 91 },
    { label: "1Y", days: 365 }
  ];
  var PERIOD_TEXT = { 30: "1 mo", 91: "3 mo", 182: "6 mo", 365: "1 yr" };

  var reducedMotion = false;
  try {
    reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  } catch (e) { /* no matchMedia: keep animations on */ }

  var gradientUid = 0;

  /* ==== formatting helpers ==== */

  function fmt2(v) {
    return v.toLocaleString("en-GB", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function fmt4(v) {
    return v.toLocaleString("en-GB", { minimumFractionDigits: 4, maximumFractionDigits: 4 });
  }

  function fmtAmount(v) {
    return v.toLocaleString("en-GB", { maximumFractionDigits: 2 });
  }

  /* signed, 2 decimals, thousands separators: "+4.41", "-1,204.20" */
  function signed2(v) {
    return (v < 0 ? "-" : "+") + fmt2(Math.abs(v));
  }

  /* rate line number: about 4 meaningful decimals, no trailing zeros */
  function fmtRate(r) {
    if (!isFinite(r)) return String(r);
    if (Math.abs(r) >= 1) return String(Number(r.toFixed(4)));
    return String(Number(r.toPrecision(4)));
  }

  /* "9 Jul 26" */
  function fmtDate(unixSeconds) {
    return new Date(unixSeconds * 1000).toLocaleDateString("en-GB", {
      day: "numeric",
      month: "short",
      year: "2-digit"
    });
  }

  /* Parse a user-typed amount. Accepts "." and "," as decimal separators;
   * strips regular, thin, narrow and no-break spaces. When both separators
   * appear, the last one wins as the decimal point and the other is
   * treated as a thousands separator. Returns NaN when unparseable. */
  function parseAmount(raw) {
    if (raw == null) return NaN;
    var s = String(raw).replace(/[\s\u00a0\u2009\u202f]/g, "");
    if (!s) return NaN;
    var lastComma = s.lastIndexOf(",");
    var lastDot = s.lastIndexOf(".");
    if (lastComma > -1 && lastDot > -1) {
      if (lastComma > lastDot) {
        s = s.replace(/\./g, "").replace(/,/g, ".");
      } else {
        s = s.replace(/,/g, "");
      }
    } else if (lastComma > -1) {
      s = s.replace(/,/g, ".");
    }
    var n = Number(s);
    return isFinite(n) ? n : NaN;
  }

  /* ==== series helpers ==== */

  /* Read and validate data-series: {"t":[unix...],"v":[float...]}.
   * Returns {t,v} with only finite, paired points, or null. */
  function parseSeries(el) {
    var raw = el.dataset ? el.dataset.series : null;
    if (!raw) return null;
    var parsed;
    try {
      parsed = JSON.parse(raw);
    } catch (e) {
      return null;
    }
    if (!parsed || !Array.isArray(parsed.t) || !Array.isArray(parsed.v)) return null;
    var n = Math.min(parsed.t.length, parsed.v.length);
    var t = [];
    var v = [];
    for (var i = 0; i < n; i++) {
      var ti = Number(parsed.t[i]);
      var vi = Number(parsed.v[i]);
      if (isFinite(ti) && isFinite(vi)) {
        t.push(ti);
        v.push(vi);
      }
    }
    return t.length >= 2 ? { t: t, v: v } : null;
  }

  /* Slice to the last `days` days; always keeps at least 2 points. */
  function sliceSeries(series, days) {
    var cutoff = Date.now() / 1000 - days * DAY;
    var i = 0;
    while (i < series.t.length && series.t[i] < cutoff) i++;
    if (series.t.length - i < 2) i = series.t.length - 2;
    if (i < 0) i = 0;
    return { t: series.t.slice(i), v: series.v.slice(i) };
  }

  /* v -> 1/v for the currency swap; drops non-finite results. */
  function invertSeries(series) {
    var t = [];
    var v = [];
    for (var i = 0; i < series.t.length; i++) {
      var iv = 1 / series.v[i];
      if (isFinite(iv)) {
        t.push(series.t[i]);
        v.push(iv);
      }
    }
    return t.length >= 2 ? { t: t, v: v } : null;
  }

  function svgEl(name, attrs) {
    var el = document.createElementNS(SVG_NS, name);
    for (var k in attrs) {
      if (Object.prototype.hasOwnProperty.call(attrs, k)) el.setAttribute(k, attrs[k]);
    }
    return el;
  }

  /* ==== shared chart widget ====
   *
   * Builds, inside `container`: a range-pill row, then a wrapper holding
   * the svg (area fill + line + crosshair) and a tooltip div.
   *
   * cfg:
   *   ranges       [{label, days}] (first entry is the default)
   *   formatValue  function(value) -> tooltip value text
   *   periodText   optional function(days) -> muted label next to the pills
   *   onRange      optional function(first, last, days), fired on every
   *                (re)render with the sliced range's endpoints
   *
   * Returns { setSeries(series) } for instant rerenders at the current range.
   */
  function buildChart(container, series, cfg) {
    var gradId = "cael-grad-" + (++gradientUid);
    var state = {
      series: series,
      days: cfg.ranges[0].days,
      sliced: null,
      px: [],
      py: []
    };

    container.classList.add("cael-chart");

    /* pills row */
    var head = document.createElement("div");
    head.className = "cael-chart-head";
    var pills = [];

    function makePill(range) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "cael-pill";
      b.textContent = range.label;
      b.setAttribute("aria-pressed", "false");
      b.addEventListener("click", function () {
        if (state.days === range.days) return;
        state.days = range.days;
        for (var j = 0; j < pills.length; j++) {
          var on = pills[j] === b;
          pills[j].classList.toggle("selected", on);
          pills[j].setAttribute("aria-pressed", on ? "true" : "false");
        }
        render(false);
      });
      pills.push(b);
      return b;
    }

    for (var r = 0; r < cfg.ranges.length; r++) {
      head.appendChild(makePill(cfg.ranges[r]));
    }
    pills[0].classList.add("selected");
    pills[0].setAttribute("aria-pressed", "true");

    var periodEl = null;
    if (cfg.periodText) {
      periodEl = document.createElement("span");
      periodEl.className = "cael-chart-period";
      head.appendChild(periodEl);
    }

    /* svg + tooltip */
    var wrap = document.createElement("div");
    wrap.className = "cael-chart-wrap";

    var svg = svgEl("svg", {
      viewBox: "0 0 " + VB_W + " " + VB_H,
      preserveAspectRatio: "none",
      width: "100%"
    });

    var defs = svgEl("defs", {});
    var grad = svgEl("linearGradient", { id: gradId, x1: "0", y1: "0", x2: "0", y2: "1" });
    grad.appendChild(svgEl("stop", { offset: "0", "stop-color": "currentColor", "stop-opacity": "0.22" }));
    grad.appendChild(svgEl("stop", { offset: "1", "stop-color": "currentColor", "stop-opacity": "0" }));
    defs.appendChild(grad);
    svg.appendChild(defs);

    var area = svgEl("path", { fill: "url(#" + gradId + ")" });
    var line = svgEl("path", {
      fill: "none",
      stroke: "currentColor",
      "stroke-width": "2",
      "stroke-linecap": "round",
      "stroke-linejoin": "round",
      "vector-effect": "non-scaling-stroke"
    });
    var cross = svgEl("g", { "class": "cael-cross" });
    var crossLine = svgEl("line", {
      x1: "0", x2: "0", y1: "0", y2: String(VB_H),
      "vector-effect": "non-scaling-stroke"
    });
    /* Ellipse, not circle: preserveAspectRatio "none" stretches the two
     * axes unevenly, so the radii are corrected per-axis on pointer move
     * to keep the dot visually round. */
    var dot = svgEl("ellipse", { cx: "0", cy: "0", rx: "4", ry: "4" });
    cross.appendChild(crossLine);
    cross.appendChild(dot);
    svg.appendChild(area);
    svg.appendChild(line);
    svg.appendChild(cross);

    var tip = document.createElement("div");
    tip.className = "cael-tip";
    var tipDate = document.createElement("span");
    tipDate.className = "cael-tip-date";
    var tipValue = document.createElement("span");
    tipValue.className = "cael-tip-value";
    tip.appendChild(tipDate);
    tip.appendChild(tipValue);

    wrap.appendChild(svg);
    wrap.appendChild(tip);
    container.appendChild(head);
    container.appendChild(wrap);

    function hideCross() {
      cross.classList.remove("show");
      tip.classList.remove("show");
    }

    function render(animate) {
      /* cancel any in-flight entrance animation */
      line.style.transition = "";
      line.style.strokeDasharray = "";
      line.style.strokeDashoffset = "";

      var s = sliceSeries(state.series, state.days);
      state.sliced = s;
      var n = s.t.length;

      var t0 = s.t[0];
      var tSpan = s.t[n - 1] - t0 || 1;
      var vmin = Infinity;
      var vmax = -Infinity;
      for (var i = 0; i < n; i++) {
        if (s.v[i] < vmin) vmin = s.v[i];
        if (s.v[i] > vmax) vmax = s.v[i];
      }
      if (vmax - vmin < 1e-12) {
        var pad = Math.abs(vmax) * 0.05 || 1;
        vmin -= pad;
        vmax += pad;
      }

      var padTop = 10;
      var padBot = 10;
      var px = new Array(n);
      var py = new Array(n);
      var d = "";
      for (i = 0; i < n; i++) {
        px[i] = ((s.t[i] - t0) / tSpan) * VB_W;
        py[i] = padTop + (1 - (s.v[i] - vmin) / (vmax - vmin)) * (VB_H - padTop - padBot);
        d += (i === 0 ? "M" : "L") + px[i].toFixed(2) + " " + py[i].toFixed(2);
      }
      state.px = px;
      state.py = py;

      line.setAttribute("d", d);
      area.setAttribute("d", d + "L" + px[n - 1].toFixed(2) + " " + VB_H + "L" + px[0].toFixed(2) + " " + VB_H + "Z");

      var up = s.v[n - 1] >= s.v[0];
      wrap.classList.toggle("up", up);
      wrap.classList.toggle("down", !up);

      hideCross();

      if (periodEl) periodEl.textContent = cfg.periodText(state.days);
      if (cfg.onRange) cfg.onRange(s.v[0], s.v[n - 1], state.days);

      if (animate && !reducedMotion) drawIn();
    }

    /* entrance: the line draws itself in ~450ms via dashoffset */
    function drawIn() {
      var len;
      try {
        len = line.getTotalLength();
      } catch (e) {
        return;
      }
      if (!isFinite(len) || len <= 0) return;
      line.style.transition = "none";
      line.style.strokeDasharray = len + " " + len;
      line.style.strokeDashoffset = String(len);
      line.getBoundingClientRect(); /* flush so the transition starts from len */
      line.style.transition = "stroke-dashoffset 450ms cubic-bezier(0.2, 0.8, 0.2, 1)";
      line.style.strokeDashoffset = "0";
      window.setTimeout(function () {
        line.style.transition = "";
        line.style.strokeDasharray = "";
        line.style.strokeDashoffset = "";
      }, 520);
    }

    function onPoint(ev) {
      if (!state.sliced) return;
      var rect = wrap.getBoundingClientRect();
      if (!rect.width || !rect.height) return;

      var frac = (ev.clientX - rect.left) / rect.width;
      if (frac < 0) frac = 0;
      if (frac > 1) frac = 1;

      /* nearest data point by x fraction */
      var targetX = frac * VB_W;
      var best = 0;
      var bestDist = Infinity;
      for (var i = 0; i < state.px.length; i++) {
        var dist = Math.abs(state.px[i] - targetX);
        if (dist < bestDist) {
          bestDist = dist;
          best = i;
        }
      }

      var x = state.px[best];
      var y = state.py[best];
      crossLine.setAttribute("x1", x.toFixed(2));
      crossLine.setAttribute("x2", x.toFixed(2));
      dot.setAttribute("cx", x.toFixed(2));
      dot.setAttribute("cy", y.toFixed(2));
      dot.setAttribute("rx", ((4 * VB_W) / rect.width).toFixed(3));
      dot.setAttribute("ry", ((4 * VB_H) / rect.height).toFixed(3));
      cross.classList.add("show");

      tipDate.textContent = fmtDate(state.sliced.t[best]);
      tipValue.textContent = cfg.formatValue(state.sliced.v[best]);
      tip.classList.add("show");

      /* place the tooltip beside the point; flip near the right edge,
       * clamp inside the wrapper so it never overflows the card */
      var sx = (x / VB_W) * rect.width;
      var sy = (y / VB_H) * rect.height;
      var tw = tip.offsetWidth;
      var th = tip.offsetHeight;
      var left = sx + 14;
      if (left + tw > rect.width - 6) left = sx - 14 - tw;
      if (left < 6) left = 6;
      var top = sy - th - 12;
      if (top < 4) top = sy + 12;
      if (top + th > rect.height - 4) top = rect.height - th - 4;
      if (top < 4) top = 4;
      tip.style.left = left + "px";
      tip.style.top = top + "px";
    }

    wrap.addEventListener("pointermove", onPoint);
    wrap.addEventListener("pointerdown", onPoint);
    wrap.addEventListener("pointerleave", hideCross);
    wrap.addEventListener("pointercancel", hideCross);

    render(true);

    return {
      setSeries: function (next) {
        if (!next) return;
        state.series = next;
        render(false);
      }
    };
  }

  /* ==== stock card ==== */

  function setDirectText(el, text) {
    for (var i = 0; i < el.childNodes.length; i++) {
      var c = el.childNodes[i];
      if (c.nodeType === 3 && c.nodeValue.replace(/\s/g, "") !== "") {
        c.nodeValue = text;
        return;
      }
    }
    el.appendChild(document.createTextNode(text));
  }

  function updateChip(chip, first, last) {
    if (!chip) return;
    var change = last - first;
    var pct = first !== 0 ? (change / first) * 100 : 0;
    var up = change >= 0;
    var icon = chip.querySelector(".cael-msr");
    if (icon) icon.textContent = up ? "trending_up" : "trending_down";
    setDirectText(chip, signed2(change) + " (" + signed2(pct) + "%)");
    chip.classList.toggle("up", up);
    chip.classList.toggle("down", !up);
  }

  function initStock(root) {
    var chartBox = root.querySelector(".cael-stock-chart");
    var series = parseSeries(root);
    if (!chartBox || !series) return; /* keep the server-rendered fallback */

    var chip = root.querySelector(".cael-stock-change");
    var currency = root.getAttribute("data-currency") || "";

    /* the live period label next to the pills replaces the static one */
    var staticPeriod = root.querySelector(".cael-stock-period");
    if (staticPeriod && staticPeriod.parentNode) {
      staticPeriod.parentNode.removeChild(staticPeriod);
    }

    chartBox.textContent = ""; /* drop the fallback sparkline svg */
    buildChart(chartBox, series, {
      ranges: STOCK_RANGES,
      formatValue: function (v) {
        return fmt2(v) + (currency ? " " + currency : "");
      },
      periodText: function (days) {
        return PERIOD_TEXT[days] || days + " d";
      },
      onRange: function (first, last) {
        updateChip(chip, first, last);
      }
    });
  }

  /* ==== currency card ==== */

  function codeSpanFor(input) {
    var el = input.nextElementSibling;
    return el && el.classList.contains("cael-cur-code") ? el : null;
  }

  function initCurrency(root) {
    /* rate lives in this closure from init on; the attribute is never
     * re-read, so swaps keep full precision */
    var state = {
      from: root.getAttribute("data-from") || "",
      to: root.getAttribute("data-to") || "",
      rate: parseFloat(root.getAttribute("data-rate"))
    };
    var rateOk = isFinite(state.rate) && state.rate !== 0;

    var fromIn = null;
    var toIn = null;
    var inputs = root.querySelectorAll(".cael-cur-in");
    for (var i = 0; i < inputs.length; i++) {
      var side = inputs[i].getAttribute("data-side");
      if (side === "from") fromIn = inputs[i];
      else if (side === "to") toIn = inputs[i];
    }

    /* chart is optional: no data-series (or no container) means no chart,
     * no pills; the converter below still works */
    var chart = null;
    var series = parseSeries(root);
    var chartBox = root.querySelector(".cael-currency-chart");
    if (series && chartBox) {
      chartBox.textContent = "";
      chart = buildChart(chartBox, series, {
        ranges: CURRENCY_RANGES,
        formatValue: function (v) {
          return fmt4(v) + (state.to ? " " + state.to : "");
        }
      });
    }

    if (fromIn && toIn && rateOk) {
      fromIn.addEventListener("input", function () {
        var n = parseAmount(fromIn.value);
        toIn.value = isFinite(n) ? fmtAmount(n * state.rate) : "";
      });
      toIn.addEventListener("input", function () {
        var n = parseAmount(toIn.value);
        fromIn.value = isFinite(n) ? fmtAmount(n / state.rate) : "";
      });
    }

    var swapBtn = root.querySelector(".cael-cur-swap");
    if (swapBtn && fromIn && toIn && rateOk) {
      swapBtn.addEventListener("click", function () {
        var tmpVal = fromIn.value;
        fromIn.value = toIn.value;
        toIn.value = tmpVal;

        var fromCode = codeSpanFor(fromIn);
        var toCode = codeSpanFor(toIn);
        if (fromCode && toCode) {
          var tmpCode = fromCode.textContent;
          fromCode.textContent = toCode.textContent;
          toCode.textContent = tmpCode;
        }

        state.rate = 1 / state.rate;
        var tmpCur = state.from;
        state.from = state.to;
        state.to = tmpCur;

        var sub = root.querySelector(".cael-hero-sub");
        if (sub) {
          sub.textContent = "1 " + state.from + " = " + fmtRate(state.rate) + " " + state.to;
        }

        if (chart) {
          var inverted = invertSeries(series);
          if (inverted) {
            series = inverted;
            chart.setSeries(inverted);
          }
        }
      });
    }
  }

  /* ==== init ==== */

  function initAll() {
    var i;
    var stocks = document.querySelectorAll("[data-cael-stock]");
    for (i = 0; i < stocks.length; i++) {
      try {
        initStock(stocks[i]);
      } catch (e) { /* leave that card's server fallback untouched */ }
    }
    var currencies = document.querySelectorAll("[data-cael-currency]");
    for (i = 0; i < currencies.length; i++) {
      try {
        initCurrency(currencies[i]);
      } catch (e) { /* same */ }
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initAll);
  } else {
    initAll();
  }
})();
