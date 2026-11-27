/* DocuMind app: upload -> instant preview -> background indexing -> chat. */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var PDFJS = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/";
  var state = { items: [], activeKey: null, scope: "doc", busy: false, zoom: 1, page: 1, pdf: null, loadId: 0, seq: 0 };

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

  function markReady(it, doc) {
    it.status = "ready"; it.doc = doc;
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
        state.items.push({ key: d.doc_id, name: d.doc_name, status: "ready", doc: d, buf: null, blobUrl: null, thread: el("div", "thread"), introEl: null });
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
      intro.appendChild(el("p", null, "Answers come with an explanation, a memory tip and page links that open the PDF at that page."));
      var chips = el("div", "chips");
      ["Summarize this document", "What are the key points?", "What should I remember for an exam?"].forEach(function (q) {
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

  var form = $("chat-form"), input = $("chat-input"), sendBtn = $("send-btn");
  function renderInput() {
    var it = active();
    var ok = it && it.status === "ready" && !state.busy;
    input.disabled = !(it && it.status === "ready");
    sendBtn.disabled = !ok;
    input.placeholder = !it ? "Ask about this PDF" : it.status === "ready" ? "Ask about " + trimName(it.name) : it.status === "failed" ? "This PDF could not be read" : "Reading the PDF\u2026";
  }

  function inline(parent, text, byPassage) {
    String(text).split(/(\[Passage\s+\d+\]|\*\*[^*]+\*\*)/g).forEach(function (part) {
      if (!part) return;
      var m = /^\[Passage\s+(\d+)\]$/.exec(part);
      if (m) {
        var c = byPassage[parseInt(m[1], 10)];
        if (!c) return;
        var pill = el("button", "cite", "p." + c.page_number);
        pill.type = "button";
        pill.title = c.doc_name + ", page " + c.page_number;
        pill.addEventListener("click", function () { openCitation(c); });
        parent.appendChild(pill);
      } else if (/^\*\*[^*]+\*\*$/.test(part)) {
        parent.appendChild(el("strong", null, part.slice(2, -2)));
      } else parent.appendChild(document.createTextNode(part));
    });
  }
  function section(cls, label) { var s = el("div", "sec " + cls); s.appendChild(el("div", "sec-label", label)); return s; }

  function renderAnswer(res) {
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
    if (res.citations && res.citations.length) {
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
    var meta = el("div", "meta");
    meta.appendChild(el("span", null, (res.latency_ms / 1000).toFixed(1) + "s"));
    if (res.provider) meta.appendChild(el("span", null, res.provider === "groq" ? "Groq" : "Gemini"));
    meta.appendChild(el("span", null, "From your document only"));
    card.appendChild(meta);
    wrap.appendChild(card);
    return wrap;
  }

  function renderError(text) {
    var wrap = el("div", "msg bot"), card = el("div", "card error"), s = el("div", "sec");
    s.appendChild(el("div", "text", text)); card.appendChild(s); wrap.appendChild(card);
    return wrap;
  }
  function typingNode() {
    var wrap = el("div", "msg bot"), card = el("div", "card typing");
    card.setAttribute("aria-label", "Thinking");
    card.appendChild(el("i")); card.appendChild(el("i")); card.appendChild(el("i"));
    wrap.appendChild(card);
    return wrap;
  }

  function ask(question) {
    question = (question || "").trim();
    var it = active();
    if (!question || state.busy || !it || it.status !== "ready") return;
    if (it.introEl) { it.introEl.remove(); it.introEl = null; }
    var u = el("div", "msg user");
    u.appendChild(el("div", "bubble", question));
    var thread = it.thread;
    thread.appendChild(u);
    var typing = typingNode();
    thread.appendChild(typing);
    messages.scrollTop = messages.scrollHeight;
    input.value = ""; autosize();
    state.busy = true; renderInput();

    var payload = { question: question };
    if (state.scope === "doc" || readyItems().length < 2) payload.doc_id = it.key;
    fetch("/api/chat", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) })
      .then(function (r) { return r.json().then(function (body) { return { ok: r.ok, body: body }; }); })
      .then(function (res) {
        typing.remove();
        var node;
        if (!res.ok) {
          var d = res.body && res.body.detail;
          node = renderError(typeof d === "string" ? d : "Something went wrong. Please try again.");
        } else node = renderAnswer(res.body);
        thread.appendChild(node);
        if (thread.parentNode === messages) messages.scrollTop = Math.max(0, node.offsetTop - 14);
      })
      .catch(function () { typing.remove(); thread.appendChild(renderError("Could not reach the server. It may be waking up, try again in a moment.")); })
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
