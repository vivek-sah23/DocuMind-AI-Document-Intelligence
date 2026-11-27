/* DocuMind workspace: documents, PDF preview (pdf.js), structured chat. */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var state = { docs: [], activeId: null, scope: "doc", pdf: null, zoom: 1, loadId: 0, page: 1, dirty: false, busy: false };

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function narrow() { return window.matchMedia("(max-width: 1180px)").matches; }

  /* ---------- tabs (tablet / mobile) ---------- */
  var panes = $("panes"), tabbar = $("tabbar");
  function setTab(name) {
    panes.setAttribute("data-active", name);
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
      var badge = $("status-badge");
      var banner = $("banner");
      if (h.llm_configured) {
        badge.className = "status ok";
        $("status-text").textContent = h.groq_configured ? "Online · Groq fast mode" : "Online · Gemini";
        banner.hidden = true;
      } else {
        badge.className = "status bad";
        $("status-text").textContent = "No API key";
        banner.hidden = false;
        banner.textContent = "The server has no LLM API key yet, so answers are disabled. Indexing and preview still work.";
      }
    }).catch(function () {
      $("status-text").textContent = "Waking up";
      if (!retry) setTimeout(function () { loadHealth(true); }, 4000);
    });
  }

  /* ---------- documents ---------- */
  function activeDoc() {
    return state.docs.filter(function (d) { return d.doc_id === state.activeId; })[0] || null;
  }

  function refreshDocuments(selectId) {
    return fetch("/api/documents").then(function (r) { return r.json(); }).then(function (data) {
      state.docs = data.documents || [];
      var ids = state.docs.map(function (d) { return d.doc_id; });
      var want = selectId || state.activeId;
      if (ids.indexOf(want) === -1) want = ids.length ? ids[ids.length - 1] : null;
      var changed = want !== state.activeId;
      state.activeId = want;
      renderDocs();
      renderScope();
      if (changed) loadPreview(activeDoc());
    }).catch(function () {});
  }

  function renderDocs() {
    var list = $("doc-list");
    list.innerHTML = "";
    $("doc-count").textContent = state.docs.length;
    $("doc-empty").style.display = state.docs.length ? "none" : "block";
    state.docs.forEach(function (doc) {
      var li = el("li", "doc-item" + (doc.doc_id === state.activeId ? " active" : ""));
      li.tabIndex = 0;
      li.setAttribute("role", "button");
      li.appendChild(el("span", "doc-ico", "PDF"));
      var meta = el("div", "doc-meta");
      var name = el("div", "doc-name", doc.doc_name);
      name.title = doc.doc_name;
      meta.appendChild(name);
      meta.appendChild(el("div", "doc-stats", doc.page_count + " pages \u00b7 " + doc.size));
      var del = el("button", "doc-del", "\u00d7");
      del.type = "button";
      del.title = "Remove document";
      del.setAttribute("aria-label", "Remove " + doc.doc_name);
      del.addEventListener("click", function (e) { e.stopPropagation(); deleteDocument(doc); });
      li.appendChild(meta);
      li.appendChild(del);
      function pick() { selectDoc(doc.doc_id, true); }
      li.addEventListener("click", pick);
      li.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); } });
      list.appendChild(li);
    });
  }

  function renderScope() {
    var doc = activeDoc();
    $("scope-name").textContent = doc ? doc.doc_name : "No document selected";
    $("scope").hidden = state.docs.length < 2;
    Array.prototype.forEach.call($("scope").querySelectorAll(".seg"), function (b) {
      b.classList.toggle("active", b.getAttribute("data-scope") === state.scope);
    });
    var hasDocs = state.docs.length > 0;
    var title = $("empty-title"), sub = $("empty-sub"), chips = $("chips");
    if (hasDocs) {
      title.textContent = "Ask anything about your PDF";
      sub.textContent = "Answers come with an explanation, a memory tip and page citations you can open.";
      chips.hidden = false;
      if (!chips.children.length) {
        ["Summarize this document", "What are the key points?", "What should I remember for an exam?"].forEach(function (q) {
          var c = el("button", "chip", q);
          c.type = "button";
          c.addEventListener("click", function () { ask(q); });
          chips.appendChild(c);
        });
      }
    } else {
      title.textContent = "Upload a PDF to begin";
      sub.textContent = "Then ask anything about it. Every answer comes with an explanation, a memory tip and page citations.";
      chips.hidden = true;
    }
  }

  function selectDoc(id, userAction) {
    if (id === state.activeId && state.pdf) { if (userAction && narrow()) setTab("preview"); return; }
    state.activeId = id;
    renderDocs();
    renderScope();
    loadPreview(activeDoc());
    if (userAction && narrow()) setTab("preview");
  }

  function deleteDocument(doc) {
    if (!window.confirm("Remove " + doc.doc_name + " from the index?")) return;
    fetch("/api/documents/" + doc.doc_id, { method: "DELETE" }).then(function () {
      if (state.activeId === doc.doc_id) state.activeId = null;
      return refreshDocuments();
    }).catch(function () {});
  }

  /* ---------- upload ---------- */
  var fileInput = $("file-input"), dropzone = $("dropzone"), uploadStatus = $("upload-status");
  function setStatus(text, kind, spin) {
    uploadStatus.hidden = false;
    uploadStatus.className = "upload-status " + (kind || "");
    uploadStatus.innerHTML = "";
    if (spin) uploadStatus.appendChild(el("span", "spinner"));
    uploadStatus.appendChild(el("span", null, text));
  }

  function uploadFile(file) {
    if (!file) return;
    if (!/\.pdf$/i.test(file.name) && file.type !== "application/pdf") {
      setStatus("Only PDF files are supported.", "error");
      return;
    }
    var form = new FormData();
    form.append("file", file);
    setStatus("Indexing " + file.name + "\u2026 large files can take a moment.", "", true);
    fetch("/api/documents", { method: "POST", body: form }).then(function (r) {
      return r.json().then(function (body) { return { ok: r.ok, body: body }; });
    }).then(function (res) {
      if (!res.ok) { setStatus(res.body.detail || "Upload failed", "error"); return; }
      var msg = "Indexed " + res.body.doc_name + " (" + res.body.page_count + " pages).";
      var kind = "ok";
      if (res.body.scanned_pages && res.body.scanned_pages.length) {
        msg += " Pages " + res.body.scanned_pages.join(", ") + " look scanned and may be missing text.";
        kind = "error";
      }
      setStatus(msg, kind);
      return refreshDocuments(res.body.doc_id).then(function () {
        if (narrow()) setTab("chat");
      });
    }).catch(function () { setStatus("Upload failed: server unreachable.", "error"); });
  }

  fileInput.addEventListener("change", function () { uploadFile(fileInput.files[0]); fileInput.value = ""; });
  dropzone.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); } });
  ["dragenter", "dragover"].forEach(function (evt) {
    dropzone.addEventListener(evt, function (e) { e.preventDefault(); dropzone.classList.add("drag"); });
  });
  ["dragleave", "drop"].forEach(function (evt) {
    dropzone.addEventListener(evt, function (e) { e.preventDefault(); dropzone.classList.remove("drag"); });
  });
  dropzone.addEventListener("drop", function (e) { uploadFile(e.dataTransfer.files && e.dataTransfer.files[0]); });

  /* ---------- preview ---------- */
  var pvBody = $("pv-body"), pvPages = $("pv-pages"), pvEmpty = $("pv-empty");
  var observer = null;

  function fileUrl(doc) { return "/api/documents/" + doc.doc_id + "/file"; }

  function resetPreview() {
    state.pdf = null;
    state.page = 1;
    if (observer) { observer.disconnect(); observer = null; }
    pvPages.innerHTML = "";
  }

  function loadPreview(doc, startPage) {
    var myLoad = ++state.loadId;
    resetPreview();
    if (!doc) {
      pvEmpty.style.display = "flex";
      $("pv-tools").hidden = true;
      $("pv-name").textContent = "Nothing selected";
      return Promise.resolve();
    }
    pvEmpty.style.display = "none";
    $("pv-name").textContent = doc.doc_name;
    $("pv-name").title = doc.doc_name;
    $("pv-open").href = fileUrl(doc);
    $("pv-tools").hidden = false;
    $("pv-total").textContent = doc.page_count;
    $("pv-page").max = doc.page_count;
    $("pv-page").value = 1;
    pvPages.appendChild(el("div", "pv-loading", "Loading preview\u2026"));

    if (!window.pdfjsLib) { iframeFallback(doc, startPage); return Promise.resolve(); }
    window.pdfjsLib.GlobalWorkerOptions.workerSrc = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js";
    return window.pdfjsLib.getDocument(fileUrl(doc)).promise.then(function (pdf) {
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
      var box = el("div", "pv-error", "This preview is not available. ");
      var a = el("a", null, "Open the PDF in a new tab");
      a.href = fileUrl(doc); a.target = "_blank"; a.rel = "noopener";
      a.style.color = "var(--brand)"; a.style.fontWeight = "600";
      box.appendChild(a);
      pvPages.appendChild(box);
    });
  }

  function iframeFallback(doc, page) {
    pvPages.innerHTML = "";
    pvPages.style.padding = "0";
    pvPages.style.height = "100%";
    var f = el("iframe", "pv-frame");
    f.title = doc.doc_name;
    f.src = fileUrl(doc) + "#page=" + (page || 1);
    pvPages.appendChild(f);
  }

  function pageWidth() { return Math.max(200, (pvBody.clientWidth - 28) * state.zoom); }

  function layoutPreview(startPage) {
    if (!state.pdf) return;
    if (pvBody.clientWidth === 0) { state.dirty = true; return; }
    state.dirty = false;
    var keep = typeof startPage === "number" ? startPage : state.page;
    var width = pageWidth();
    var height = Math.round(width * state.aspect);
    var myLoad = state.loadId;
    if (observer) observer.disconnect();
    pvPages.innerHTML = "";
    observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (en.isIntersecting) renderPage(en.target, myLoad);
      });
    }, { root: pvBody, rootMargin: "700px 0px" });
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
      var scale = cssW / base.width;
      var ratio = Math.min(window.devicePixelRatio || 1, 2);
      var vp = page.getViewport({ scale: scale * ratio });
      var canvas = document.createElement("canvas");
      canvas.width = Math.floor(vp.width);
      canvas.height = Math.floor(vp.height);
      box.style.height = Math.round(cssW * base.height / base.width) + "px";
      return page.render({ canvasContext: canvas.getContext("2d"), viewport: vp }).promise.then(function () {
        if (myLoad !== state.loadId) return;
        box.appendChild(canvas);
      });
    }).catch(function () { box.dataset.done = ""; });
  }

  function jumpToPage(n, instant) {
    if (!state.pdf) return;
    n = Math.max(1, Math.min(state.pdf.numPages, n || 1));
    var box = pvPages.querySelector('[data-page="' + n + '"]');
    if (!box) return;
    pvBody.scrollTo({ top: box.offsetTop - 10, behavior: instant ? "auto" : "smooth" });
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
      var top = pvBody.scrollTop + pvBody.clientHeight * 0.3;
      var boxes = pvPages.children, cur = 1;
      for (var i = 0; i < boxes.length; i++) { if (boxes[i].offsetTop <= top) cur = i + 1; else break; }
      state.page = cur;
      $("pv-page").value = cur;
    });
  }, { passive: true });

  $("pv-prev").addEventListener("click", function () { jumpToPage(state.page - 1); });
  $("pv-next").addEventListener("click", function () { jumpToPage(state.page + 1); });
  $("pv-page").addEventListener("change", function () { jumpToPage(parseInt(this.value, 10) || 1); });
  function zoom(delta) {
    state.zoom = Math.max(0.6, Math.min(2.5, +(state.zoom + delta).toFixed(2)));
    layoutPreview();
  }
  $("pv-in").addEventListener("click", function () { zoom(0.2); });
  $("pv-out").addEventListener("click", function () { zoom(-0.2); });
  var resizeTimer = null;
  window.addEventListener("resize", function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () { layoutPreview(); }, 150);
  });

  function openCitation(c) {
    var doc = state.docs.filter(function (d) { return d.doc_id === c.doc_id; })[0];
    if (!doc) return;
    if (narrow()) setTab("preview");
    if (doc.doc_id !== state.activeId || !state.pdf) {
      state.activeId = doc.doc_id;
      renderDocs();
      renderScope();
      loadPreview(doc, c.page_number);
    } else {
      requestAnimationFrame(function () { jumpToPage(c.page_number); });
    }
  }

  /* ---------- chat rendering ---------- */
  var messages = $("messages");
  function scrollDown() { messages.scrollTop = messages.scrollHeight; }
  function hideEmpty() { var e = $("empty-state"); if (e) e.style.display = "none"; }

  /* Inline text: **bold** and [Passage N] tags become citation pills. */
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
      } else {
        parent.appendChild(document.createTextNode(part));
      }
    });
  }

  function section(cls, label) {
    var s = el("div", "sec " + cls);
    s.appendChild(el("div", "sec-label", label));
    return s;
  }

  function renderAnswer(res) {
    var wrap = el("div", "msg bot");
    var card = el("div", "card" + (res.refused ? " refused" : ""));
    var byPassage = {};
    (res.citations || []).forEach(function (c) { byPassage[c.passage] = c; });

    var a = section("sec-answer", res.refused ? "Not in the document" : "Answer");
    var t = el("div", "text");
    inline(t, res.answer, byPassage);
    a.appendChild(t);
    card.appendChild(a);

    if (res.explanation) {
      var e = section("sec-explain", "Explanation");
      var et = el("div", "text");
      inline(et, res.explanation, byPassage);
      e.appendChild(et);
      card.appendChild(e);
    }
    if (res.key_points && res.key_points.length) {
      var k = section("sec-points", "Key points");
      var ul = el("ul", "points");
      res.key_points.forEach(function (p) { var li = el("li"); inline(li, p, byPassage); ul.appendChild(li); });
      k.appendChild(ul);
      card.appendChild(k);
    }
    if (res.memory_tip) {
      var m = section("sec-tip", "Memory tip");
      var mt = el("div", "text");
      inline(mt, res.memory_tip, byPassage);
      m.appendChild(mt);
      card.appendChild(m);
    }
    if (res.citations && res.citations.length) {
      var s = section("sec-sources", "Sources");
      var row = el("div", "sources");
      var detail = el("div", "src-detail");
      detail.hidden = true;
      res.citations.forEach(function (c) {
        var b = el("button", "src-btn");
        b.type = "button";
        b.appendChild(el("span", "nm", c.doc_name));
        b.appendChild(el("span", null, "p." + c.page_number));
        b.appendChild(el("span", "sc", Math.round(c.score * 100) + "%"));
        b.title = "Open page " + c.page_number + " in the preview";
        b.addEventListener("click", function () {
          detail.hidden = false;
          detail.textContent = c.text;
          openCitation(c);
        });
        row.appendChild(b);
      });
      s.appendChild(row);
      s.appendChild(detail);
      card.appendChild(s);
    }
    var meta = el("div", "meta");
    meta.appendChild(el("span", null, (res.latency_ms / 1000).toFixed(1) + "s"));
    if (res.provider) meta.appendChild(el("span", null, res.provider === "groq" ? "Groq" : "Gemini"));
    meta.appendChild(el("span", null, "Answered only from your document"));
    card.appendChild(meta);
    wrap.appendChild(card);
    return wrap;
  }

  function renderError(text) {
    var wrap = el("div", "msg bot");
    var card = el("div", "card error");
    var s = el("div", "sec");
    s.appendChild(el("div", "text", text));
    card.appendChild(s);
    wrap.appendChild(card);
    return wrap;
  }

  function typingNode() {
    var wrap = el("div", "msg bot");
    var card = el("div", "card typing");
    card.setAttribute("aria-label", "Thinking");
    card.appendChild(el("i")); card.appendChild(el("i")); card.appendChild(el("i"));
    wrap.appendChild(card);
    return wrap;
  }

  /* ---------- chat ---------- */
  var form = $("chat-form"), input = $("chat-input"), sendBtn = $("send-btn");

  function ask(question) {
    question = (question || "").trim();
    if (!question || state.busy) return;
    if (!state.docs.length) {
      messages.appendChild(renderError("Upload a PDF first so there is something to search."));
      hideEmpty(); scrollDown();
      if (narrow()) setTab("docs");
      return;
    }
    hideEmpty();
    var u = el("div", "msg user");
    u.appendChild(el("div", "bubble", question));
    messages.appendChild(u);
    var typing = typingNode();
    messages.appendChild(typing);
    scrollDown();
    input.value = "";
    autosize();
    state.busy = true;
    sendBtn.disabled = true;

    var payload = { question: question };
    if (state.scope === "doc" && state.activeId) payload.doc_id = state.activeId;
    fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    }).then(function (r) {
      return r.json().then(function (body) { return { ok: r.ok, body: body }; });
    }).then(function (res) {
      typing.remove();
      var node;
      if (!res.ok) {
        var d = res.body && res.body.detail;
        node = renderError(typeof d === "string" ? d : "Something went wrong. Please try again.");
      } else {
        node = renderAnswer(res.body);
      }
      messages.appendChild(node);
      messages.scrollTop = Math.max(0, node.offsetTop - messages.offsetTop - 12);
    }).catch(function () {
      typing.remove();
      messages.appendChild(renderError("Could not reach the server. It may be waking up; try again in a moment."));
      scrollDown();
    }).finally(function () { state.busy = false; sendBtn.disabled = false; input.focus({ preventScroll: true }); });
  }

  form.addEventListener("submit", function (e) { e.preventDefault(); ask(input.value); });
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); ask(input.value); }
  });
  function autosize() { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 140) + "px"; }
  input.addEventListener("input", autosize);

  $("scope").addEventListener("click", function (e) {
    var b = e.target.closest(".seg");
    if (!b) return;
    state.scope = b.getAttribute("data-scope");
    renderScope();
  });

  /* ---------- boot ---------- */
  loadHealth();
  refreshDocuments();
})();
