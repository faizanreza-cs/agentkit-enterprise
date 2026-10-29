/* Project site behaviour. No framework, and nothing depends on it: without this file every
   section is still there, just without the motion. */
(function () {
  "use strict";
  var d = document, root = d.documentElement;
  var reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var raf = window.requestAnimationFrame.bind(window);

  function clamp(v, lo, hi) { return Math.min(hi, Math.max(lo, v)); }
  function all(sel, el) { return Array.prototype.slice.call((el || d).querySelectorAll(sel)); }
  function safely(fn) { try { fn(); } catch (e) { if (window.console) console.error(e); } }

  // Loader: a short counter, then the hero rises (only when the page asked for it, see index.html)
  safely(function () {
    var loader = d.querySelector(".loader");
    if (!loader) return;
    if (!root.classList.contains("loading")) { loader.parentNode.removeChild(loader); return; }
    var num = loader.querySelector(".loader-num"), bar = loader.querySelector(".loader-bar i");
    var start = performance.now(), length = 900, done = false;
    function finish() {
      if (done) return;
      done = true;
      loader.classList.add("done");
      root.classList.add("anim");
      setTimeout(function () {
        root.classList.remove("loading");
        if (loader.parentNode) loader.parentNode.removeChild(loader);
      }, 900);
    }
    function tick(now) {
      var p = clamp((now - start) / length, 0, 1), eased = 1 - Math.pow(1 - p, 3);
      num.textContent = String(Math.round(eased * 100));
      bar.style.width = (eased * 100).toFixed(1) + "%";
      if (p < 1) raf(tick); else setTimeout(finish, 160);
    }
    raf(tick);
    setTimeout(finish, 2400);
  });

  // Reveal on scroll: whatever is already in view stays visible, the rest rises as it arrives
  safely(function () {
    var items = all(".reveal");
    if (reduce || !items.length || !("IntersectionObserver" in window)) return;
    var vh = window.innerHeight;
    function settle(el) {
      // once it has risen, hand transitions back to the element's own styles (hover lifts)
      var delay = parseFloat(getComputedStyle(el).getPropertyValue("--d")) || 0;
      setTimeout(function () { el.classList.remove("reveal", "in"); }, 1100 + delay * 1000);
    }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        entry.target.classList.add("in");
        io.unobserve(entry.target);
        settle(entry.target);
      });
    }, { rootMargin: "0px 0px -8% 0px", threshold: 0.06 });
    items.forEach(function (el) {
      var r = el.getBoundingClientRect();
      if (r.top < vh && r.bottom > 0) el.classList.remove("reveal");
      else io.observe(el);
    });
    root.classList.add("reveal-on");
  });

  // The overview paragraph lights up word by word
  var lit = d.querySelector(".lit");
  var words = lit && !reduce ? all(".w", lit) : [];
  if (words.length) lit.classList.add("lit-on");
  function updateLit() {
    var r = lit.getBoundingClientRect(), vh = window.innerHeight;
    var from = vh * 0.85, to = vh * 0.3;
    var p = clamp((from - r.top) / (r.height + from - to), 0, 1);
    var n = Math.round(p * words.length);
    for (var i = 0; i < words.length; i++) words[i].classList.toggle("on", i < n);
  }

  // How it works: the section is pinned and its cards slide sideways (wide screens only)
  var how = d.querySelector(".how"), pin = how && how.querySelector(".pin");
  var track = how && how.querySelector(".steps-track");
  var wide = window.matchMedia("(min-width: 900px) and (min-height: 560px)");
  var distance = 0;
  function layoutPin() {
    if (!pin) return;
    if (reduce || !wide.matches) {
      how.classList.remove("is-pinned");
      pin.style.height = "";
      track.style.transform = "";
      distance = 0;
      return;
    }
    how.classList.add("is-pinned");
    distance = Math.max(0, track.offsetWidth - root.clientWidth);
    pin.style.height = (window.innerHeight + distance) + "px";
  }
  function updatePin() {
    var p = clamp(-pin.getBoundingClientRect().top / distance, 0, 1);
    track.style.transform = "translate3d(" + (-p * distance).toFixed(1) + "px,0,0)";
  }

  // The results panel grows into place
  var panel = d.querySelector(".panel");
  function updatePanel() {
    var r = panel.getBoundingClientRect(), vh = window.innerHeight;
    var p = clamp((vh - r.top) / (vh * 0.9), 0, 1);
    panel.style.setProperty("--s", (0.93 + 0.07 * p).toFixed(4));
  }

  // The nav stays out of the way over the hero
  var nav = d.querySelector(".nav");

  var queued = false;
  function frame() {
    queued = false;
    if (nav && !reduce) nav.classList.toggle("is-hidden", window.scrollY < 80);
    if (words.length) updateLit();
    if (distance) updatePin();
    if (panel && !reduce) updatePanel();
  }
  function onScroll() { if (!queued) { queued = true; raf(frame); } }
  function relayout() { safely(layoutPin); frame(); }
  window.addEventListener("scroll", onScroll, { passive: true });
  window.addEventListener("resize", relayout);
  window.addEventListener("load", relayout);
  if (wide.addEventListener) wide.addEventListener("change", relayout);
  if (d.fonts && d.fonts.ready) d.fonts.ready.then(relayout);
  relayout();

  // Copy buttons on the commands
  safely(function () {
    if (!navigator.clipboard || !window.isSecureContext) return;
    all(".copy").forEach(function (button) {
      var label = button.textContent;
      button.hidden = false;
      button.addEventListener("click", function () {
        var code = button.closest("li").querySelector("code").textContent;
        navigator.clipboard.writeText(code).then(function () { say(button, "Copied"); },
          function () { say(button, "Select it"); });
      });
      function say(b, text) { b.textContent = text; setTimeout(function () { b.textContent = label; }, 1600); }
    });
  });

  // A ring that follows the mouse
  safely(function () {
    if (reduce || !window.matchMedia("(hover: hover) and (pointer: fine)").matches) return;
    var ring = d.createElement("div");
    ring.className = "cursor";
    ring.setAttribute("aria-hidden", "true");
    ring.appendChild(d.createElement("i"));
    d.body.appendChild(ring);
    var x = 0, y = 0, tx = 0, ty = 0, moving = false, seen = false;
    function follow() {
      x += (tx - x) * 0.22;
      y += (ty - y) * 0.22;
      ring.style.transform = "translate3d(" + x.toFixed(1) + "px," + y.toFixed(1) + "px,0)";
      if (Math.abs(tx - x) + Math.abs(ty - y) > 0.4) raf(follow); else moving = false;
    }
    d.addEventListener("pointermove", function (e) {
      if (e.pointerType !== "mouse") return;
      tx = e.clientX; ty = e.clientY;
      if (!seen) { x = tx; y = ty; seen = true; }
      ring.classList.add("on");
      if (!moving) { moving = true; raf(follow); }
    }, { passive: true });
    d.addEventListener("pointerover", function (e) {
      var t = e.target;
      ring.classList.toggle("big", !!(t.closest && t.closest("a, button, summary, label")));
    });
    root.addEventListener("mouseleave", function () { ring.classList.remove("on"); });
  });
})();
