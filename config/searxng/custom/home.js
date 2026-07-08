/* caelestia start page behaviour: hero clock + weather chip.
 *
 * Vanilla JS, no dependencies. Included with `defer` by the
 * templates/simple/index.html override, so it only runs on the home page;
 * every DOM lookup is guarded anyway.
 */
(function () {
  "use strict";

  /* Copenhagen */
  var WEATHER_LAT = 55.6761;
  var WEATHER_LON = 12.5683;

  var CLOCK_TICK_MS = 15000;

  /* ==== clock ==== */

  function pad2(n) {
    return (n < 10 ? "0" : "") + n;
  }

  function formatDate(now) {
    /* "Wednesday 9 July" */
    try {
      return now.toLocaleDateString("en-GB", {
        weekday: "long",
        day: "numeric",
        month: "long"
      });
    } catch (e) {
      return now.toDateString();
    }
  }

  function updateClock() {
    var clockEl = document.getElementById("cael-home-clock");
    var dateEl = document.getElementById("cael-home-date");
    if (!clockEl && !dateEl) {
      return;
    }
    var now = new Date();
    if (clockEl) {
      clockEl.textContent = pad2(now.getHours()) + ":" + pad2(now.getMinutes());
    }
    if (dateEl) {
      dateEl.textContent = formatDate(now);
    }
    var wrap = document.getElementById("cael-home-clockwrap");
    if (wrap) {
      wrap.classList.add("cael-home-time-ready");
    }
  }

  /* ==== weather ==== */

  /* WMO weather code -> Material Symbols Rounded ligature */
  function wmoIcon(code) {
    if (code === 0) return "clear_day";
    if (code === 1 || code === 2) return "partly_cloudy_day";
    if (code === 3) return "cloud";
    if (code === 45 || code === 48) return "foggy";
    if ((code >= 51 && code <= 67) || (code >= 80 && code <= 82)) return "rainy";
    if ((code >= 71 && code <= 77) || code === 85 || code === 86) return "weather_snowy";
    if (code >= 95 && code <= 99) return "thunderstorm";
    return null;
  }

  function initWeather() {
    var chip = document.getElementById("cael-home-weather");
    var iconEl = document.getElementById("cael-home-weather-icon");
    var tempEl = document.getElementById("cael-home-weather-temp");
    if (!chip || !iconEl || !tempEl || typeof window.fetch !== "function") {
      return;
    }
    var url =
      "https://api.open-meteo.com/v1/forecast" +
      "?latitude=" + WEATHER_LAT +
      "&longitude=" + WEATHER_LON +
      "&current=temperature_2m,weather_code";
    window
      .fetch(url)
      .then(function (resp) {
        if (!resp.ok) {
          throw new Error("open-meteo HTTP " + resp.status);
        }
        return resp.json();
      })
      .then(function (data) {
        var cur = data && data.current;
        if (!cur || typeof cur.temperature_2m !== "number") {
          return;
        }
        var icon = wmoIcon(cur.weather_code);
        if (!icon) {
          return;
        }
        iconEl.textContent = icon;
        tempEl.textContent = Math.round(cur.temperature_2m) + "°";
        chip.hidden = false;
      })
      .catch(function () {
        /* offline / blocked / bad payload: chip stays hidden, silently */
      });
  }

  /* ==== boot ==== */

  function init() {
    updateClock();
    window.setInterval(updateClock, CLOCK_TICK_MS);
    initWeather();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    /* `defer` scripts normally run before DOMContentLoaded, but guard the
       already-parsed case (e.g. dynamic injection) too */
    init();
  }
})();
