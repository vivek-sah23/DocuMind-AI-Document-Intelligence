/* DocuMind app: upload -> instant preview -> background indexing -> chat. */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var PDFJS = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/";
  var state = { items: [], activeKey: null, scope: "doc", deep: false, voice: false, busy: false, zoom: 1, page: 1, pdf: null, loadId: 0, seq: 0 };

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function narrow() { return window.matchMedia("(max-width: 960px)").matches; }
  function active() { return state.items.filter(function (i) { return i.key === state.activeKey; })[0] || null; }
  function byKey(k) { return state.items.filter(function (i) { return i.key === k; })[0] || null; }
  function readyItems() { return state.items.filter(function (i) { return i.status === "ready"; }); }

  var toastTimer = null;
  function toast(msg) {
    var t = $("toast");
    t.textContent = msg; t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.hidden = true; }, 4200);
  }

  /* ---------- tabs (phone / tablet) ---------- */
  var work = $("work"), tabbar = $("tabbar");
  function setTab(name) {
    work.setAttribute("data-active", name);
    Array.prototype.forEach.call(tabbar.querySelectorAll("button"), function (b) {
      b.classList.toggle("active", b.getAttribute("data-tab") === name);
    });
    if (name === "preview") requestAnimationFrame(function () { layoutPreview(); });
  }
  tabbar.addEventListener("click", function (e) {
    var b = e.target.closest("button");
    if (b) setTab(b.getAttribute("data-tab"));
  });

  /* ---------- health ---------- */
  function loadHealth(retry) {
    fetch("/api/health").then(function (r) { return r.json(); }).then(function (h) {
      var badge = $("status-badge"), banner = $("banner");
      if (h.llm_configured) {
        badge.className = "status ok";
        $("status-text").textContent = h.groq_configured ? "Fast mode" : "Online";
        banner.hidden = true;
      } else {
        badge.className = "status bad";
        $("status-text").textContent = "No API key";
        banner.hidden = false;
        banner.textContent = "The server has no LLM key yet, so answers are off. Upload and preview still work.";
      }
    }).catch(function () {
      $("status-text").textContent = "Waking up";
      if (!retry) setTimeout(function () { loadHealth(true); }, 4000);
    });
  }

  /* ---------- screen state ---------- */
  function render() {
    var any = state.items.length > 0;
    $("start").hidden = any;
    work.hidden = !any;
    $("file-switch").hidden = !any;
    tabbar.hidden = !any;
    $("shell").setAttribute("data-state", any ? "work" : "empty");
    renderChip();
    renderMenu();
    renderThread();
    renderScope();
    renderInput();
  }

  function stateLabel(it) {
    if (it.status === "ready") return "Ready";
    if (it.status === "failed") return "Failed";
    return it.status === "uploading" ? "Uploading" : "Reading";
  }

  function renderChip() {
    var it = active();
    if (!it) return;
    $("chip-name").textContent = it.name;
    $("chip-name").title = it.name;
    var s = $("chip-state");
    s.className = "chip-state " + it.status;
    s.innerHTML = "";
    if (it.status === "indexing" || it.status === "uploading") s.appendChild(el("span", "spinner"));
    s.appendChild(el("span", null, stateLabel(it)));
  }

  function renderMenu() {
    var ul = $("menu-list");
    ul.innerHTML = "";
    state.items.forEach(function (it) {
      var li = el("li", it.key === state.activeKey ? "active" : "");
      var b = el("button", "mi");
      b.type = "button";
      b.appendChild(el("span", "mi-ico", "PDF"));
      b.appendChild(el("span", "mi-name", it.name));
      b.appendChild(el("span", "mi-meta", it.status === "ready" && it.doc ? it.doc.page_count + " pp" : stateLabel(it)));
      b.addEventListener("click", function () { closeMenu(); selectItem(it.key); });
      var d = el("button", "mi-del", "\u00d7");
      d.type = "button";
      d.setAttribute("aria-label", "Remove " + it.name);
      d.addEventListener("click", function (e) { e.stopPropagation(); removeItem(it); });
      li.appendChild(b); li.appendChild(d);
      ul.appendChild(li);
    });
  }

  var chip = $("file-chip"), menu = $("menu");
  function closeMenu() { menu.hidden = true; chip.setAttribute("aria-expanded", "false"); }
  chip.addEventListener("click", function (e) {
    e.stopPropagation();
    var open = menu.hidden;
    menu.hidden = !open;
    chip.setAttribute("aria-expanded", open ? "true" : "false");
  });
  document.addEventListener("click", function (e) { if (!menu.contains(e.target)) closeMenu(); });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") closeMenu(); });

  function removeItem(it) {
    if (!window.confirm("Remove " + it.name + "?")) return;
    var done = function () {
      state.items = state.items.filter(function (x) { return x !== it; });
      if (state.activeKey === it.key) {
        state.activeKey = null;
        var next = state.items[state.items.length - 1];
        if (next) selectItem(next.key); else { resetPreview(); render(); }
      } else render();
    };
    var local = String(it.key).indexOf("l") === 0;
    it.removed = true;
    try { localStorage.removeItem(STORE + it.key); } catch (e) {}
    if (local) { done(); return; }
    fetch("/api/documents/" + it.key, { method: "DELETE" }).then(done, done);
  }

  /* ---------- upload ---------- */
  var fileInput = $("file-input"), dropzone = $("dropzone");

  function startError(msg) {
    var old = document.querySelector(".start-err");
    if (old) old.remove();
    if (!msg) return;
    var e = el("p", "start-err", msg);
    e.setAttribute("role", "alert");
    $("dropzone").insertAdjacentElement("afterend", e);
  }

  function addFile(file) {
    if (!file) return;
    startError("");
    var isPdf = /\.pdf$/i.test(file.name) || file.type === "application/pdf";
    if (!isPdf) { var m = "That is not a PDF. Pick a .pdf file."; startError(m); toast(m); return; }
    if (file.size > 50 * 1024 * 1024) { var m2 = "That file is over 50 MB."; startError(m2); toast(m2); return; }
    var it = { key: "l" + (++state.seq), name: file.name, status: "uploading", doc: null, buf: null, blobUrl: null, thread: el("div", "thread"), introEl: null, error: null };
    state.items.push(it);
    state.activeKey = it.key;
    render();
    if (narrow()) setTab("preview");

    file.arrayBuffer().then(function (buf) {
      it.buf = buf;
      it.blobUrl = URL.createObjectURL(new Blob([buf], { type: "application/pdf" }));
      if (state.activeKey === it.key) loadPreview(it);
    });

    var form = new FormData();
    form.append("file", file);
    fetch("/api/documents", { method: "POST", body: form }).then(function (r) {
      return r.json().then(function (body) { return { ok: r.ok, body: body }; });
    }).then(function (res) {
      if (it.removed) return;
      if (!res.ok) { fail(it, (res.body && res.body.detail) || "Upload failed."); return; }
      var existing = byKey(res.body.doc_id);
      if (existing && existing !== it) {
        state.items = state.items.filter(function (x) { return x !== it; });
        toast("That PDF is already open.");
        selectItem(existing.key);
        return;
      }
      var wasActive = state.activeKey === it.key;
      it.key = res.body.doc_id;
      if (wasActive) state.activeKey = it.key;
      if (res.body.status === "ready") { markReady(it, res.body.document); return; }
      it.status = "indexing";
      render();
      poll(it);
    }).catch(function () { fail(it, "Upload failed. The server may be waking up, try again in a moment."); });
  }

  function fail(it, msg) {
    it.status = "failed"; it.error = msg;
    render();
    toast(msg);
  }

  /* ---------- saved chats: kept per PDF in this browser only (localStorage) ---------- */
  var STORE = "documind:chat:v1:";
  function slimRes(res) {
    var c = JSON.parse(JSON.stringify(res));
    (c.citations || []).forEach(function (x) { if (x.text && x.text.length > 700) x.text = x.text.slice(0, 700) + "\u2026"; });
    return c;
  }
  function saveChat(it) {
    if (!it || String(it.key).indexOf("l") === 0) return;
    var log = (it.log || []).slice(-40);
    for (var tries = 0; tries < 6; tries++) {
      try { localStorage.setItem(STORE + it.key, JSON.stringify({ entries: log, history: (it.history || []).slice(-12) })); return; }
      catch (e) { log = log.slice(Math.ceil(log.length / 2)); if (!log.length) return; }
    }
  }
  function userBubble(question, mode) {
    var u = el("div", "msg user");
    u.appendChild(el("div", "bubble", (mode === "deep" ? "Deep dive: " : "") + question));
    return u;
  }
  function restoreChat(it) {
    if (!it || it.restored || String(it.key).indexOf("l") === 0) return;
    it.restored = true;
    var data = null;
    try { data = JSON.parse(localStorage.getItem(STORE + it.key) || "null"); } catch (e) {}
    if (!data || !data.entries) return;
    it.log = data.entries; it.history = data.history || [];
    it.log.forEach(function (e) {
      try { it.thread.appendChild(userBubble(e.q, e.mode)); it.thread.appendChild(renderAnswer(e.res, e.q)); } catch (err) {}
    });
  }

  function markReady(it, doc) {
    it.status = "ready"; it.doc = doc;
    restoreChat(it);
    if (it.name !== doc.doc_name) it.name = doc.doc_name;
    if (state.activeKey === it.key) {
      $("pv-total").textContent = doc.page_count;
      $("pv-page").max = doc.page_count;
    }
    render();
    if (doc.scanned_pages && doc.scanned_pages.length) {
      toast("Pages " + shortList(doc.scanned_pages) + " have no readable text.");
    }
  }
  function shortList(a) { return a.length > 6 ? a.slice(0, 6).join(", ") + "\u2026" : a.join(", "); }

  function poll(it) {
    if (it.removed) return;
    fetch("/api/documents/" + it.key + "/status").then(function (r) {
      if (r.status === 404) throw new Error("gone");
      return r.json();
    }).then(function (s) {
      if (it.removed) return;
      if (s.status === "ready") markReady(it, s.document);
      else if (s.status === "failed") fail(it, s.error || "Could not read this PDF.");
      else setTimeout(function () { poll(it); }, 1200);
    }).catch(function () {
      if (it.removed) return;
      it.pollErrors = (it.pollErrors || 0) + 1;
      if (it.pollErrors > 8) fail(it, "Lost contact with the server while reading the PDF. Try uploading again.");
      else setTimeout(function () { poll(it); }, 2500);
    });
  }

  fileInput.addEventListener("change", function () { addFile(fileInput.files[0]); fileInput.value = ""; });
  dropzone.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); } });
  ["dragenter", "dragover"].forEach(function (evt) {
    document.addEventListener(evt, function (e) { e.preventDefault(); dropzone.classList.add("drag"); });
  });
  ["dragleave", "drop"].forEach(function (evt) {
    document.addEventListener(evt, function (e) { e.preventDefault(); if (evt === "drop" || e.target === document.documentElement) dropzone.classList.remove("drag"); });
  });
  document.addEventListener("drop", function (e) { addFile(e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0]); });

  /* ---------- load existing documents (after a refresh) ---------- */
  function loadExisting() {
    return fetch("/api/documents").then(function (r) { return r.json(); }).then(function (data) {
      (data.documents || []).forEach(function (d) {
        if (byKey(d.doc_id)) return;
        var ni = { key: d.doc_id, name: d.doc_name, status: "ready", doc: d, buf: null, blobUrl: null, thread: el("div", "thread"), introEl: null };
        state.items.push(ni);
        restoreChat(ni);
      });
      if (state.items.length) {
        var saved = null;
        try { saved = localStorage.getItem("documind:last"); } catch (e) {}
        var pick = (saved && byKey(saved)) || state.items[state.items.length - 1];
        selectItem(pick.key);
      } else render();
    }).catch(function () { render(); });
  }

  function selectItem(key) {
    var it = byKey(key);
    if (!it) return;
    var same = state.activeKey === key && state.pdf;
    state.activeKey = key;
    try { if (String(key).indexOf("l") !== 0) localStorage.setItem("documind:last", key); } catch (e) {}
    render();
    if (!same) loadPreview(it);
  }

  /* ---------- preview (pdf.js) ---------- */
  var pvBody = $("pv-body"), pvPages = $("pv-pages"), observer = null;

  function fileUrl(it) { return it.blobUrl || ("/api/documents/" + it.key + "/file"); }

  function resetPreview() {
    state.pdf = null; state.page = 1; state.loadId++;
    if (observer) { observer.disconnect(); observer = null; }
    pvPages.innerHTML = "";
  }

  function loadPreview(it, startPage) {
    var myLoad = ++state.loadId;
    if (observer) { observer.disconnect(); observer = null; }
    state.pdf = null; state.page = 1; state.zoom = 1;
    pvPages.innerHTML = "";
    if (!it) return;
    $("pv-open").href = fileUrl(it);
    $("pv-total").textContent = it.doc ? it.doc.page_count : "\u2026";
    $("pv-page").value = 1;
    if (it.status === "uploading" && !it.buf) { pvPages.appendChild(el("div", "pv-loading", "Opening your PDF\u2026")); return; }
    pvPages.appendChild(el("div", "pv-loading", "Opening your PDF\u2026"));

    if (!window.pdfjsLib) { setTimeout(function () { if (state.loadId === myLoad) loadPreview(it, startPage); }, 400); return; }
    window.pdfjsLib.GlobalWorkerOptions.workerSrc = PDFJS + "pdf.worker.min.js";
    var src = it.buf ? { data: new Uint8Array(it.buf.slice(0)) } : { url: fileUrl(it) };
    window.pdfjsLib.getDocument(src).promise.then(function (pdf) {
      if (myLoad !== state.loadId) return;
      state.pdf = pdf;
      $("pv-total").textContent = pdf.numPages;
      $("pv-page").max = pdf.numPages;
      return pdf.getPage(1).then(function (p1) {
        if (myLoad !== state.loadId) return;
        state.aspect = p1.view[3] / p1.view[2] || 1.294;
        layoutPreview(startPage);
      });
    }).catch(function () {
      if (myLoad !== state.loadId) return;
      pvPages.innerHTML = "";
      var box = el("div", "pv-error", "The preview could not load. ");
      var a = el("a", null, "Open the PDF in a new tab");
      a.href = fileUrl(it); a.target = "_blank"; a.rel = "noopener"; a.style.fontWeight = "600";
      box.appendChild(a);
      pvPages.appendChild(box);
    });
  }

  function pageWidth() { return Math.max(200, Math.min(pvBody.clientWidth - 28, 900) * state.zoom); }

  function layoutPreview(startPage) {
    if (!state.pdf || pvBody.clientWidth === 0) return;
    var keep = typeof startPage === "number" ? startPage : state.page;
    var width = pageWidth(), height = Math.round(width * state.aspect), myLoad = state.loadId;
    if (observer) observer.disconnect();
    pvPages.innerHTML = "";
    observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) { if (en.isIntersecting) renderPage(en.target, myLoad); });
    }, { root: pvBody, rootMargin: "900px 0px" });
    for (var i = 1; i <= state.pdf.numPages; i++) {
      var box = el("div", "pv-page-box");
      box.dataset.page = i;
      box.style.width = width + "px";
      box.style.height = height + "px";
      pvPages.appendChild(box);
      observer.observe(box);
    }
    jumpToPage(keep, true);
  }

  function renderPage(box, myLoad) {
    if (box.dataset.done || myLoad !== state.loadId || !state.pdf) return;
    box.dataset.done = "1";
    var n = parseInt(box.dataset.page, 10);
    state.pdf.getPage(n).then(function (page) {
      if (myLoad !== state.loadId) return;
      var base = page.getViewport({ scale: 1 });
      var cssW = parseFloat(box.style.width);
      var ratio = Math.min(window.devicePixelRatio || 1, 2);
      var vp = page.getViewport({ scale: (cssW / base.width) * ratio });
      var canvas = document.createElement("canvas");
      canvas.width = Math.floor(vp.width);
      canvas.height = Math.floor(vp.height);
      box.style.height = Math.round(cssW * base.height / base.width) + "px";
      return page.render({ canvasContext: canvas.getContext("2d"), viewport: vp }).promise.then(function () {
        if (myLoad === state.loadId) box.appendChild(canvas);
      });
    }).catch(function () { box.dataset.done = ""; });
  }

  function jumpToPage(n, instant) {
    if (!state.pdf) return;
    n = Math.max(1, Math.min(state.pdf.numPages, n || 1));
    var box = pvPages.querySelector('[data-page="' + n + '"]');
    if (!box) return;
    pvBody.style.scrollBehavior = instant ? "auto" : "smooth";
    pvBody.scrollTo({ top: box.offsetTop - 12 });
    state.page = n;
    $("pv-page").value = n;
    if (!instant) { box.classList.remove("flash"); void box.offsetWidth; box.classList.add("flash"); }
  }

  var ticking = false;
  pvBody.addEventListener("scroll", function () {
    if (ticking || !state.pdf) return;
    ticking = true;
    requestAnimationFrame(function () {
      ticking = false;
      var top = pvBody.scrollTop + pvBody.clientHeight * 0.35, boxes = pvPages.children, cur = 1;
      for (var i = 0; i < boxes.length; i++) { if (boxes[i].offsetTop <= top) cur = i + 1; else break; }
      state.page = cur;
      $("pv-page").value = cur;
    });
  }, { passive: true });

  $("pv-prev").addEventListener("click", function () { jumpToPage(state.page - 1); });
  $("pv-next").addEventListener("click", function () { jumpToPage(state.page + 1); });
  $("pv-page").addEventListener("change", function () { jumpToPage(parseInt(this.value, 10) || 1); });
  function zoom(d) { state.zoom = Math.max(0.6, Math.min(2.5, +(state.zoom + d).toFixed(2))); layoutPreview(); }
  $("pv-in").addEventListener("click", function () { zoom(0.2); });
  $("pv-out").addEventListener("click", function () { zoom(-0.2); });
  var rt = null;
  window.addEventListener("resize", function () { clearTimeout(rt); rt = setTimeout(function () { layoutPreview(); }, 160); });

  function openCitation(c) {
    var it = byKey(c.doc_id);
    if (!it) return;
    if (narrow()) setTab("preview");
    if (it.key !== state.activeKey || !state.pdf) {
      state.activeKey = it.key;
      render();
      loadPreview(it, c.page_number);
    } else {
      requestAnimationFrame(function () { jumpToPage(c.page_number); });
    }
  }

  /* ---------- chat thread ---------- */
  var messages = $("messages");

  function renderThread() {
    var it = active();
    messages.innerHTML = "";
    if (!it) return;
    messages.appendChild(it.thread);
    // intro block reflects the file's state
    if (it.introEl) it.introEl.remove();
    var intro = el("div", "intro");
    if (it.status === "failed") {
      intro.appendChild(el("h3", null, "This PDF could not be read"));
      intro.appendChild(el("div", "fail", it.error || "Something went wrong."));
    } else if (it.status !== "ready") {
      intro.appendChild(el("h3", null, "Reading your PDF"));
      intro.appendChild(el("p", null, "You can already scroll through it on the left. Questions open up as soon as it is indexed."));
      var pr = el("div", "progress");
      pr.appendChild(el("span", "bar-anim"));
      pr.appendChild(el("span", null, it.status === "uploading" ? "Uploading\u2026" : "Indexing\u2026"));
      intro.appendChild(pr);
    } else if (!it.thread.querySelector(".msg")) {
      intro.appendChild(el("h3", null, "Ask anything about " + trimName(it.name)));
      intro.appendChild(el("p", null, "Chat like you would with a tutor. It explains from the basics, pinpoints exact details with page links, remembers what you asked, and says so when the PDF does not cover something. Tap Listen to hear any answer."));
      var chips = el("div", "chips");
      ["Explain the main idea of this document from the basics", "Summarize this document", "What should I remember for an exam?"].forEach(function (q) {
        var c = el("button", "chip", q);
        c.type = "button";
        c.addEventListener("click", function () { ask(q); });
        chips.appendChild(c);
      });
      intro.appendChild(chips);
    } else { intro = null; }
    if (intro) { messages.insertBefore(intro, it.thread); it.introEl = intro; } else it.introEl = null;
    messages.scrollTop = messages.scrollHeight;
  }
  function trimName(n) { n = n.replace(/\.pdf$/i, ""); return n.length > 36 ? n.slice(0, 34) + "\u2026" : n; }

  function renderScope() {
    $("scope").hidden = readyItems().length < 2;
    Array.prototype.forEach.call($("scope").querySelectorAll(".seg"), function (b) {
      b.classList.toggle("active", b.getAttribute("data-scope") === state.scope);
    });
  }
  $("scope").addEventListener("click", function (e) {
    var b = e.target.closest(".seg");
    if (!b) return;
    state.scope = b.getAttribute("data-scope");
    renderScope();
  });

  var form = $("chat-form"), input = $("chat-input"), sendBtn = $("send-btn"), deepBtn = $("deep-btn"), voiceBtn = $("voice-btn");
  voiceBtn.addEventListener("click", function () {
    state.voice = !state.voice;
    if (!state.voice && window.speechSynthesis) window.speechSynthesis.cancel();
    renderInput();
    toast(state.voice ? "Voice on: answers appear in chat and are read aloud." : "Voice off. You can still tap Listen on any answer.");
  });
  deepBtn.addEventListener("click", function () {
    state.deep = !state.deep;
    renderInput();
    toast(state.deep ? "Deep dive on: every answer is a full structured briefing." : "Tutor chat on: a conversation that remembers what you asked.");
    input.focus({ preventScroll: true });
  });
  function renderInput() {
    var it = active();
    var ok = it && it.status === "ready" && !state.busy;
    input.disabled = !(it && it.status === "ready");
    sendBtn.disabled = !ok;
    deepBtn.disabled = !(it && it.status === "ready");
    voiceBtn.hidden = !window.speechSynthesis;
    voiceBtn.disabled = deepBtn.disabled;
    voiceBtn.setAttribute("aria-pressed", state.voice ? "true" : "false");
    voiceBtn.classList.toggle("on", !!state.voice);
    deepBtn.setAttribute("aria-pressed", state.deep ? "true" : "false");
    deepBtn.classList.toggle("on", !!state.deep);
    input.placeholder = !it ? "Ask about this PDF" : it.status === "ready" ? "Ask about " + trimName(it.name) : it.status === "failed" ? "This PDF could not be read" : "Reading the PDF\u2026";
  }

  /* ---------- math: KaTeX when loaded, readable plain notation otherwise ---------- */
  var MATHS = [], pendingMath = [];
  var MATH_RE = /\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]|\\\(([\s\S]+?)\\\)|(?<!\\)\$(?=[^\s$])([^$\n]+?)(?<=[^\s$\\])\$(?!\d)/g;
  function cleanTex(t) {
    t = String(t).trim();
    var i;
    while ((i = t.indexOf("\\boxed{")) >= 0) {
      var j = i + 7, d = 1;
      while (j < t.length && d) { d += t[j] === "{" ? 1 : t[j] === "}" ? -1 : 0; j++; }
      t = t.slice(0, i) + t.slice(i + 7, j - 1) + t.slice(j);
    }
    return t.replace(/\\(?:Big|big|Bigg|bigg)[lr]?(?![a-zA-Z])/g, "").replace(/\\(?:displaystyle|;|,|!|quad|qquad)/g, " ");
  }
  var SYM = { times: "\u00d7", cdot: "\u00b7", div: "\u00f7", pm: "\u00b1", le: "\u2264", leq: "\u2264", ge: "\u2265", geq: "\u2265", ne: "\u2260", neq: "\u2260", approx: "\u2248", infty: "\u221e", lfloor: "\u230a", rfloor: "\u230b", lceil: "\u2308", rceil: "\u2309", sum: "\u03a3", prod: "\u03a0", sqrt: "\u221a", alpha: "\u03b1", beta: "\u03b2", gamma: "\u03b3", delta: "\u03b4", Delta: "\u0394", epsilon: "\u03b5", theta: "\u03b8", lambda: "\u03bb", mu: "\u03bc", pi: "\u03c0", sigma: "\u03c3", Sigma: "\u03a3", omega: "\u03c9", eta: "\u03b7", rightarrow: "\u2192", to: "\u2192", leftarrow: "\u2190", partial: "\u2202", nabla: "\u2207", log: "log", ln: "ln", exp: "exp", max: "max", min: "min" };
  function texToPlain(t) {
    t = cleanTex(t);
    for (var k = 0; k < 4; k++) {
      t = t.replace(/\\d?frac\{([^{}]*)\}\{([^{}]*)\}/g, function (m, x, y) { return "(" + x + ") / (" + y + ")"; });
      t = t.replace(/\\(?:text|mathrm|mathbf|operatorname|mathit)\{([^{}]*)\}/g, "$1");
      t = t.replace(/\\sqrt\{([^{}]*)\}/g, "\u221a($1)");
    }
    t = t.replace(/\\([a-zA-Z]+)/g, function (m, n) { return SYM[n] != null ? SYM[n] : n; });
    t = t.replace(/_\{([^{}]*)\}/g, "_$1").replace(/\^\{([^{}]*)\}/g, "^$1").replace(/[{}]/g, "").replace(/\\/g, "").replace(/\s+/g, " ");
    return t.trim();
  }
  function extractMath(text) {
    /* Code spans are literal: never treat \( \) or $ inside backticks as math. */
    return String(text).split(/(`[^`\n]*`)/g).map(function (seg, i) {
      if (i % 2 === 1) return seg;
      return seg.replace(MATH_RE, function (m, a, b, c, d) {
        var tex = a != null ? a : b != null ? b : c != null ? c : d;
        var display = a != null || b != null;
        MATHS.push({ tex: cleanTex(tex), display: display });
        return "\u0001" + (MATHS.length - 1) + "\u0002";
      });
    }).join("");
  }
  function paintMath(node, mj) {
    if (window.katex) {
      try {
        window.katex.render(mj.tex, node, { displayMode: mj.display, throwOnError: true, strict: "ignore", trust: false });
        node.classList.remove("math-plain");
        return true;
      } catch (e) { /* fall through to readable text */ }
    }
    node.textContent = texToPlain(mj.tex);
    node.classList.add("math-plain");
    return !!window.katex;
  }
  function mathNode(idx) {
    var mj = MATHS[idx];
    if (!mj) return document.createTextNode("");
    var n = el("span", "math" + (mj.display ? " math-block" : ""));
    if (!paintMath(n, mj)) pendingMath.push({ node: n, mj: mj });
    return n;
  }
  function flushMath() {
    if (!window.katex) return;
    var list = pendingMath; pendingMath = [];
    list.forEach(function (p) { paintMath(p.node, p.mj); });
  }
  window.addEventListener("load", function () { flushMath(); setTimeout(flushMath, 1500); setTimeout(flushMath, 4000); });

  function citeNums(spec) {
    var out = [];
    String(spec).split(/[,;&]|\band\b|passages?/i).forEach(function (part) {
      part = part.trim();
      var r = /^(\d+)\s*[-\u2013\u2014]\s*(\d+)$/.exec(part);
      if (r && +r[1] <= +r[2] && +r[2] - +r[1] <= 20) { for (var n = +r[1]; n <= +r[2]; n++) out.push(n); }
      else (part.match(/\d+/g) || []).forEach(function (n) { out.push(+n); });
    });
    return out;
  }
  function inline(parent, text, byPassage) {
    text = extractMath(text);
    String(text).split(/(\[Passages?\s+\d[^\]]*\]|\u0001\d+\u0002|\*\*[^*]+\*\*|`[^`\n]+`|\*[^*\s][^*\n]*\*)/g).forEach(function (part) {
      if (!part) return;
      var m = /^\[Passages?\s+(\d[^\]]*)\]$/.exec(part), mm = /^\u0001(\d+)\u0002$/.exec(part);
      if (m) {
        var seen = {};
        citeNums(m[1]).forEach(function (num) {
          var c = byPassage[num];
          if (!c || seen[num]) return;
          seen[num] = 1;
          var pill = el("button", "cite", "p." + c.page_number);
          pill.type = "button";
          pill.title = c.doc_name + ", page " + c.page_number;
          pill.addEventListener("click", function () { openCitation(c); });
          parent.appendChild(pill);
        });
      } else if (mm) {
        parent.appendChild(mathNode(+mm[1]));
      } else if (/^\*\*[^*]+\*\*$/.test(part)) {
        var st = el("strong"); inline(st, part.slice(2, -2), byPassage); parent.appendChild(st);
      } else if (/^`[^`\n]+`$/.test(part)) {
        parent.appendChild(el("code", null, part.slice(1, -1)));
      } else if (/^\*[^*\s][^*\n]*\*$/.test(part)) {
        var emn = el("em"); inline(emn, part.slice(1, -1), byPassage); parent.appendChild(emn);
      } else parent.appendChild(document.createTextNode(part.replace(/\\\$/g, "$")));
    });
  }
  /* ---------- Markdown tables ---------- */
  function splitRow(line) {
    var t = line.trim(), cells = [], cur = "", inCode = false;
    if (t.charAt(0) === "|") t = t.slice(1);
    for (var k = 0; k < t.length; k++) {
      var ch = t.charAt(k);
      if (ch === "`") inCode = !inCode;
      if (ch === "|" && !inCode && t.charAt(k - 1) !== "\\") { cells.push(cur.trim()); cur = ""; }
      else cur += ch;
    }
    if (cur.trim() !== "" || !cells.length) cells.push(cur.trim());
    return cells;
  }
  function isTableSep(line) {
    var t = (line || "").trim();
    return t.indexOf("|") >= 0 && t.indexOf("-") >= 0 && /^\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?$/.test(t);
  }
  function buildTable(rows, aligns, byPassage) {
    var wrap = el("div", "md-table-wrap"), tbl = el("table", "md-table"), thead = el("thead"), tbody = el("tbody"), n = rows[0].length;
    function fill(tr, cells, tag) {
      for (var c = 0; c < n; c++) {
        var cell = el(tag); if (aligns[c]) cell.style.textAlign = aligns[c];
        inline(cell, cells[c] == null ? "" : cells[c], byPassage); tr.appendChild(cell);
      }
    }
    var htr = el("tr"); fill(htr, rows[0], "th"); thead.appendChild(htr);
    rows.slice(1).forEach(function (r) { var tr = el("tr"); fill(tr, r, "td"); tbody.appendChild(tr); });
    tbl.appendChild(thead); tbl.appendChild(tbody); wrap.appendChild(tbl);
    return wrap;
  }
  /* Small, safe Markdown renderer for tutor answers (DOM nodes only, no innerHTML). */
  function mdBlocks(parent, text, byPassage) {
    var lines = extractMath(String(text).replace(/\r/g, "")).split("\n"), i = 0, para = [], list = null;
    function flushPara() {
      if (!para.length) return;
      var p = el("p"); inline(p, para.join(" "), byPassage); parent.appendChild(p); para = [];
    }
    function flushList() { if (list) { parent.appendChild(list.node); list = null; } }
    for (; i < lines.length; i++) {
      var ln = lines[i], t = ln.trim(), m;
      if (!t) { flushPara(); flushList(); continue; }
      if (t.indexOf("|") >= 0 && isTableSep(lines[i + 1])) {
        flushPara(); flushList();
        var head = splitRow(t), aligns = splitRow(lines[i + 1]).map(function (c) {
          return /^:-*:$/.test(c) ? "center" : /-:$/.test(c) ? "right" : "";
        }), rows = [head];
        i += 2;
        while (i < lines.length && lines[i].trim() && lines[i].indexOf("|") >= 0) { rows.push(splitRow(lines[i])); i++; }
        i--;
        parent.appendChild(buildTable(rows, aligns, byPassage));
        continue;
      }
      if (/^(-{3,}|\*{3,})$/.test(t)) { flushPara(); flushList(); parent.appendChild(el("hr")); continue; }
      if ((m = /^(#{1,4})\s+(.*)$/.exec(t))) {
        flushPara(); flushList();
        var h = el("h5", "md-h"); inline(h, m[2], byPassage); parent.appendChild(h); continue;
      }
      var b = /^[-*\u2022]\s+(.*)$/.exec(t), n = /^(\d+)[.)]\s+(.*)$/.exec(t);
      if (b || n) {
        flushPara();
        var kind = n ? "ol" : "ul";
        if (!list || list.kind !== kind) { flushList(); list = { kind: kind, node: el(kind, "md-list") }; if (n && n[1] !== "1") list.node.start = parseInt(n[1], 10); }
        var li = el("li"); inline(li, b ? b[1] : n[2], byPassage); list.node.appendChild(li);
        continue;
      }
      if (list && /^\s{2,}/.test(ln) && list.node.lastChild) { inline(list.node.lastChild, " " + t, byPassage); continue; }
      flushList();
      para.push(t);
    }
    flushPara(); flushList();
  }

  function renderTutor(res, question) {
    var wrap = el("div", "msg bot");
    var card = el("div", "card tutor" + (res.refused ? " refused" : ""));
    var byPassage = {};
    (res.citations || []).forEach(function (c) { byPassage[c.passage] = c; });
    var body = el("div", "tutor-body");
    if (res.refused) body.appendChild(el("div", "sec-label", "Not in the document"));
    mdBlocks(body, res.answer, byPassage);
    card.appendChild(body);
    appendSources(card, res);
    if (res.follow_ups && res.follow_ups.length) {
      var fu = el("div", "followups");
      fu.appendChild(el("div", "sec-label", "Keep going"));
      res.follow_ups.forEach(function (q) {
        var b = el("button", "chip fu", q);
        b.type = "button";
        b.addEventListener("click", function () { ask(q); });
        fu.appendChild(b);
      });
      card.appendChild(fu);
    }
    appendMeta(card, res);
    wrap.appendChild(card);
    return wrap;
  }

  function section(cls, label) { var s = el("div", "sec " + cls); s.appendChild(el("div", "sec-label", label)); return s; }

  /* ---------- audio briefing (browser speech synthesis, free, no keys) ---------- */
  var tts = window.speechSynthesis || null;
  var speaking = { btn: null, rate: 1 };
  function plain(t) { return String(t || "").replace(MATH_RE, function (m, a, b, c, d) { return " " + texToPlain(a || b || c || d || "") + " "; }).replace(/\[Passages?\s+\d[^\]]*\]/gi, "").replace(/^\s{0,3}#{1,4}\s+/gm, "").replace(/^\s*([-*\u2022]|\d+[.)])\s+/gm, "").replace(/^-{3,}$/gm, "").replace(/[`*]/g, "").replace(/\s+/g, " ").replace(/\s+([.,;:])/g, "$1").trim(); }
  function narration(res) {
    var parts = [];
    if (res.mode === "tutor") return plain(res.answer);
    if (res.title) parts.push(plain(res.title) + ".");
    parts.push(plain(res.answer));
    if (res.explanation) parts.push(plain(res.explanation));
    (res.sections || []).forEach(function (sec) {
      if (sec.heading) parts.push(plain(sec.heading) + ".");
      parts.push(plain(sec.body));
    });
    if (res.key_points && res.key_points.length) {
      parts.push("Key points.");
      res.key_points.forEach(function (p) { parts.push(plain(p)); });
    }
    if (res.gaps) parts.push("Not covered in the document. " + plain(res.gaps));
    return parts.filter(Boolean).join(" ");
  }
  function pickVoice() {
    var vs = tts.getVoices() || [];
    var en = vs.filter(function (v) { return /^en/i.test(v.lang); });
    var good = en.filter(function (v) { return /natural|google|samantha|aria|jenny|neural/i.test(v.name); })[0];
    return good || en.filter(function (v) { return v.default; })[0] || en[0] || null;
  }
  function stopSpeech() {
    if (!tts) return;
    tts.cancel();
    if (speaking.btn) { speaking.btn.classList.remove("on"); speaking.btn.querySelector("span").textContent = "Listen"; speaking.btn = null; }
  }
  function chunkText(text) {
    var sentences = text.match(/[^.!?]+[.!?]+(\s|$)|[^.!?]+$/g) || [text];
    var out = [], cur = "";
    sentences.forEach(function (sn) {
      if ((cur + sn).length > 220 && cur) { out.push(cur.trim()); cur = ""; }
      cur += sn;
    });
    if (cur.trim()) out.push(cur.trim());
    return out;
  }
  function speak(btn, text) {
    if (speaking.btn === btn) { stopSpeech(); return; }
    stopSpeech();
    var chunks = chunkText(text), i = 0, voice = pickVoice();
    speaking.btn = btn;
    btn.classList.add("on"); btn.querySelector("span").textContent = "Stop";
    function next() {
      if (speaking.btn !== btn || i >= chunks.length) { if (speaking.btn === btn) stopSpeech(); return; }
      var u = new SpeechSynthesisUtterance(chunks[i++]);
      if (voice) u.voice = voice;
      u.lang = voice ? voice.lang : "en-US";
      u.rate = speaking.rate;
      u.onend = next;
      u.onerror = function () { if (speaking.btn === btn) stopSpeech(); };
      tts.speak(u);
    }
    next();
  }
  function listenControls(res) {
    if (!tts || res.refused) return null;
    var box = el("div", "listen");
    var btn = el("button", "listen-btn");
    btn.type = "button";
    btn.setAttribute("aria-label", "Listen to this answer");
    btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 5 6 9H2v6h4l5 4V5z"/><path d="M15.5 8.5a5 5 0 0 1 0 7M18.5 5.5a9 9 0 0 1 0 13"/></svg><span>Listen</span>';
    btn.addEventListener("click", function () { speak(btn, narration(res)); });
    var sp = el("button", "rate-btn", "1x");
    sp.type = "button";
    sp.title = "Speaking speed";
    sp.addEventListener("click", function () {
      var rates = [1, 1.25, 1.5, 0.85];
      speaking.rate = rates[(rates.indexOf(speaking.rate) + 1) % rates.length];
      sp.textContent = speaking.rate + "x";
    });
    box.appendChild(btn); box.appendChild(sp);
    return box;
  }
  window.addEventListener("pagehide", stopSpeech);

  function renderDeep(res, question) {
    var wrap = el("div", "msg bot");
    var card = el("div", "card deep");
    var byPassage = {};
    (res.citations || []).forEach(function (c) { byPassage[c.passage] = c; });
    var head = el("div", "sec sec-answer");
    head.appendChild(el("div", "sec-label", "Deep dive"));
    head.appendChild(el("h4", "deep-title", res.title || question));
    if (res.pages && res.pages.length) head.appendChild(el("div", "deep-pages", "Draws on pages " + shortList(res.pages.map(String))));
    var ot = el("div", "text lead-text"); inline(ot, res.answer, byPassage); head.appendChild(ot);
    card.appendChild(head);
    (res.sections || []).forEach(function (sec, i) {
      var d = el("div", "sec deep-sec");
      var h = el("div", "deep-h");
      h.appendChild(el("span", "deep-n", String(i + 1)));
      h.appendChild(el("span", null, sec.heading || ("Part " + (i + 1))));
      d.appendChild(h);
      var bt = el("div", "text"); inline(bt, sec.body, byPassage); d.appendChild(bt);
      card.appendChild(d);
    });
    if (res.key_points && res.key_points.length) {
      var k = section("sec-points", "Key takeaways"), ul = el("ul", "points");
      res.key_points.forEach(function (p) { var li = el("li"); inline(li, p, byPassage); ul.appendChild(li); });
      k.appendChild(ul); card.appendChild(k);
    }
    if (res.memory_tip) {
      var m = section("sec-tip", "Memory tip");
      var mt = el("div", "text"); inline(mt, res.memory_tip, byPassage); m.appendChild(mt); card.appendChild(m);
    }
    if (res.gaps) {
      var g = section("sec-gaps", "Not covered in the PDF");
      var gt = el("div", "text"); gt.textContent = res.gaps; g.appendChild(gt); card.appendChild(g);
    }
    appendSources(card, res);
    appendMeta(card, res);
    wrap.appendChild(card);
    return wrap;
  }

  function appendSources(card, res) {
    if (!(res.citations && res.citations.length)) return;
    var s = section("sec-sources", "Sources"), row = el("div", "sources"), detail = el("div", "src-detail");
    detail.hidden = true;
    res.citations.forEach(function (c) {
      var b = el("button", "src-btn");
      b.type = "button";
      if (state.items.length > 1) b.appendChild(el("span", "nm", c.doc_name));
      b.appendChild(el("span", null, "p." + c.page_number));
      b.appendChild(el("span", "sc", Math.round(c.score * 100) + "% match"));
      b.title = "Open page " + c.page_number;
      b.addEventListener("click", function () { detail.hidden = false; detail.textContent = c.text; openCitation(c); });
      row.appendChild(b);
    });
    s.appendChild(row); s.appendChild(detail); card.appendChild(s);
  }
  function appendMeta(card, res, onDeeper) {
    var meta = el("div", "meta");
    meta.appendChild(el("span", null, (res.latency_ms / 1000).toFixed(1) + "s"));
    if (res.provider) meta.appendChild(el("span", null, res.provider === "groq" ? "Groq" : res.provider === "excerpts" ? "PDF excerpts" : "Gemini"));
    meta.appendChild(el("span", null, "From your document only"));
    var lc = listenControls(res);
    if (lc) meta.appendChild(lc);
    if (onDeeper) {
      var b = el("button", "deeper", "Explain in depth");
      b.type = "button";
      b.addEventListener("click", onDeeper);
      meta.appendChild(b);
    }
    card.appendChild(meta);
  }

  function renderAnswer(res, question) {
    if (res.mode === "tutor") return renderTutor(res, question);
    if (res.mode === "deep" && !res.refused) return renderDeep(res, question);
    var wrap = el("div", "msg bot");
    var card = el("div", "card" + (res.refused ? " refused" : ""));
    var byPassage = {};
    (res.citations || []).forEach(function (c) { byPassage[c.passage] = c; });

    var a = section("sec-answer", res.refused ? "Not in the document" : "Answer");
    var t = el("div", "text"); inline(t, res.answer, byPassage); a.appendChild(t); card.appendChild(a);
    if (res.explanation) {
      var e = section("sec-explain", "Explanation");
      var et = el("div", "text"); inline(et, res.explanation, byPassage); e.appendChild(et); card.appendChild(e);
    }
    if (res.key_points && res.key_points.length) {
      var k = section("sec-points", "Key points"), ul = el("ul", "points");
      res.key_points.forEach(function (p) { var li = el("li"); inline(li, p, byPassage); ul.appendChild(li); });
      k.appendChild(ul); card.appendChild(k);
    }
    if (res.memory_tip) {
      var m = section("sec-tip", "Memory tip");
      var mt = el("div", "text"); inline(mt, res.memory_tip, byPassage); m.appendChild(mt); card.appendChild(m);
    }
    appendSources(card, res);
    appendMeta(card, res, res.refused ? null : function () { ask(question, "deep"); });
    wrap.appendChild(card);
    return wrap;
  }

  function renderError(text) {
    var wrap = el("div", "msg bot"), card = el("div", "card error"), s = el("div", "sec");
    s.appendChild(el("div", "text", text)); card.appendChild(s); wrap.appendChild(card);
    return wrap;
  }
  function typingNode(mode) {
    var wrap = el("div", "msg bot"), card = el("div", "card typing");
    var label = mode === "deep" ? "Reading across the whole PDF\u2026" : mode === "tutor" ? "Reading the PDF and thinking\u2026" : "";
    if (label) { card.classList.add("with-label"); card.appendChild(el("span", "typing-label", label)); }
    card.setAttribute("aria-label", "Thinking");
    card.appendChild(el("i")); card.appendChild(el("i")); card.appendChild(el("i"));
    wrap.appendChild(card);
    return wrap;
  }

  var RETRY_WAIT = [3000, 6000, 10000, 15000];
  function setTypingLabel(typing, text) {
    var card = typing.querySelector(".card"), lab = typing.querySelector(".typing-label");
    if (!lab && card) { lab = el("span", "typing-label"); card.classList.add("with-label"); card.insertBefore(lab, card.firstChild); }
    if (lab) lab.textContent = text;
  }
  /* POST /api/chat. Retries cold starts, gateway errors and dropped connections so the user rarely sees a failure. */
  function chatFetch(payload, typing, tries) {
    var ctl = window.AbortController ? new AbortController() : null;
    var timer = setTimeout(function () { if (ctl) ctl.abort(); }, 100000);
    return fetch("/api/chat", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload), signal: ctl ? ctl.signal : undefined })
      .then(function (r) {
        clearTimeout(timer);
        return r.text().then(function (t) {
          var b = null; try { b = JSON.parse(t); } catch (e) {}
          return { ok: r.ok && !!b, status: r.status, body: b };
        });
      })
      .catch(function () { clearTimeout(timer); return { ok: false, status: 0, body: null }; })
      .then(function (res) {
        var transient = res.status === 0 || res.status === 502 || res.status === 503 || res.status === 504 || (res.ok === false && res.status === 200);
        if (!res.ok && transient && tries < RETRY_WAIT.length) {
          setTypingLabel(typing, tries === 0 ? "Waking things up, one moment\u2026" : "Still working, retrying\u2026");
          return new Promise(function (ok) { setTimeout(ok, RETRY_WAIT[tries]); }).then(function () { return chatFetch(payload, typing, tries + 1); });
        }
        return res;
      });
  }

  function ask(question, forceMode) {
    question = (question || "").trim();
    var it = active();
    if (!question || state.busy || !it || it.status !== "ready") return;
    if (it.introEl) { it.introEl.remove(); it.introEl = null; }
    var mode = forceMode || (state.deep ? "deep" : "tutor");
    it.history = it.history || [];
    it.log = it.log || [];
    var thread = it.thread;
    thread.appendChild(userBubble(question, mode));
    var typing = typingNode(mode);
    thread.appendChild(typing);
    messages.scrollTop = messages.scrollHeight;
    input.value = ""; autosize();
    state.busy = true; renderInput();

    var payload = { question: question, mode: mode };
    if (mode === "tutor") payload.history = it.history.slice(-8);
    if (state.scope === "doc" || readyItems().length < 2) payload.doc_id = it.key;
    chatFetch(payload, typing, 0)
      .then(function (res) {
        typing.remove();
        var node;
        if (!res.ok) {
          var d = res.body && res.body.detail;
          node = renderError(res.status === 429 && typeof d === "string" ? d : typeof d === "string" && res.status < 500 ? d : "The AI is busy right now (free models have limits). Please try again in a few seconds.");
        } else {
          node = renderAnswer(res.body, question);
          it.log.push({ q: question, mode: mode, res: slimRes(res.body) });
          if (mode === "tutor" && res.body && res.body.answer && !res.body.degraded) {
            it.history.push({ role: "user", text: question.slice(0, 1500) });
            it.history.push({ role: "assistant", text: String(res.body.answer).replace(/\[Passages?\s+\d[^\]]*\]/g, "").slice(0, 2500) });
            if (it.history.length > 12) it.history = it.history.slice(-12);
          }
          saveChat(it);
        }
        thread.appendChild(node);
        if (state.voice && res.ok) { var lb = node.querySelector(".listen-btn"); if (lb) lb.click(); }
        if (thread.parentNode === messages) messages.scrollTop = Math.max(0, node.offsetTop - 14);
      })
      .catch(function () { typing.remove(); thread.appendChild(renderError("Still could not reach the server. The free host may be waking up. Please try again in about a minute.")); })
      .then(function () { state.busy = false; renderInput(); if (!narrow()) input.focus({ preventScroll: true }); });
  }

  form.addEventListener("submit", function (e) { e.preventDefault(); ask(input.value); });
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); ask(input.value); }
  });
  function autosize() { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 140) + "px"; }
  input.addEventListener("input", autosize);

  /* ---------- boot ---------- */
  loadHealth();
  loadExisting();
})();
