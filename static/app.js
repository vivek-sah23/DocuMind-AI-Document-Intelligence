/* DocuMind frontend: upload, document list, chat with citation cards. */
(function () {
  "use strict";

  var fileInput = document.getElementById("file-input");
  var dropzone = document.getElementById("dropzone");
  var uploadStatus = document.getElementById("upload-status");
  var docList = document.getElementById("doc-list");
  var docEmpty = document.getElementById("doc-empty");
  var messages = document.getElementById("messages");
  var chatForm = document.getElementById("chat-form");
  var chatInput = document.getElementById("chat-input");
  var sendBtn = document.getElementById("send-btn");
  var keyBanner = document.getElementById("key-banner");

  /* ---------- health ---------- */
  fetch("/api/health")
    .then(function (r) { return r.json(); })
    .then(function (h) { keyBanner.hidden = h.gemini_configured; })
    .catch(function () { /* server starting up */ });

  /* ---------- documents ---------- */
  function refreshDocuments() {
    fetch("/api/documents")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var docs = data.documents || [];
        docList.innerHTML = "";
        docEmpty.style.display = docs.length ? "none" : "block";
        docs.forEach(function (doc) {
          var li = document.createElement("li");
          li.className = "doc-item";

          var dot = document.createElement("span");
          dot.className = "doc-dot";

          var meta = document.createElement("div");
          meta.className = "doc-meta";
          var name = document.createElement("div");
          name.className = "doc-name";
          name.textContent = doc.doc_name;
          name.title = doc.doc_name;
          var stats = document.createElement("div");
          stats.className = "doc-stats";
          stats.textContent =
            doc.page_count + " pages · " + doc.chunk_count + " chunks · " + doc.size;
          meta.appendChild(name);
          meta.appendChild(stats);

          var del = document.createElement("button");
          del.className = "doc-del";
          del.type = "button";
          del.title = "Remove document";
          del.textContent = "×";
          del.addEventListener("click", function () { deleteDocument(doc.doc_id); });

          li.appendChild(dot);
          li.appendChild(meta);
          li.appendChild(del);
          docList.appendChild(li);
        });
      })
      .catch(function () {});
  }

  function setStatus(text, kind) {
    uploadStatus.hidden = false;
    uploadStatus.textContent = text;
    uploadStatus.className = "upload-status " + (kind || "");
  }

  function uploadFile(file) {
    if (!file) return;
    var form = new FormData();
    form.append("file", file);
    setStatus("Indexing " + file.name + " …");
    fetch("/api/documents", { method: "POST", body: form })
      .then(function (r) {
        return r.json().then(function (body) { return { ok: r.ok, body: body }; });
      })
      .then(function (res) {
        if (!res.ok) {
          setStatus(res.body.detail || "Upload failed", "error");
          return;
        }
        setStatus(
          "Indexed " + res.body.doc_name + " — " + res.body.chunk_count + " chunks.",
          "ok"
        );
        if (res.body.scanned_pages && res.body.scanned_pages.length) {
          setStatus(
            "Indexed, but pages " + res.body.scanned_pages.join(", ") +
              " look scanned and may be missing text.",
            "error"
          );
        }
        refreshDocuments();
      })
      .catch(function () { setStatus("Upload failed: server unreachable", "error"); });
  }

  function deleteDocument(docId) {
    fetch("/api/documents/" + docId, { method: "DELETE" })
      .then(function () { refreshDocuments(); })
      .catch(function () {});
  }

  dropzone.addEventListener("click", function () { fileInput.click(); });
  fileInput.addEventListener("change", function () {
    uploadFile(fileInput.files[0]);
    fileInput.value = "";
  });
  ["dragenter", "dragover"].forEach(function (evt) {
    dropzone.addEventListener(evt, function (e) {
      e.preventDefault();
      dropzone.classList.add("drag");
    });
  });
  ["dragleave", "drop"].forEach(function (evt) {
    dropzone.addEventListener(evt, function (e) {
      e.preventDefault();
      dropzone.classList.remove("drag");
    });
  });
  dropzone.addEventListener("drop", function (e) {
    var file = e.dataTransfer.files && e.dataTransfer.files[0];
    uploadFile(file);
  });

  /* ---------- chat ---------- */
  function addMessage(text, who) {
    var wrap = document.createElement("div");
    wrap.className = "msg " + who;
    var bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = text;
    wrap.appendChild(bubble);
    messages.appendChild(wrap);
    messages.scrollTop = messages.scrollHeight;
    return bubble;
  }

  function addSources(citations) {
    if (!citations || !citations.length) return;
    var box = document.createElement("div");
    box.className = "sources";
    citations.forEach(function (c) {
      var card = document.createElement("div");
      card.className = "source";

      var head = document.createElement("div");
      head.className = "source-head";
      var chevron = document.createElement("span");
      chevron.className = "chevron";
      chevron.textContent = "▶";
      var dot = document.createElement("span");
      dot.className = "doc-dot";
      var label = document.createElement("span");
      label.textContent = c.doc_name + " · page " + c.page_number;
      var score = document.createElement("span");
      score.className = "score";
      score.textContent = c.score.toFixed(2);
      head.appendChild(chevron);
      head.appendChild(dot);
      head.appendChild(label);
      head.appendChild(score);

      var body = document.createElement("div");
      body.className = "source-body";
      body.textContent = c.text;

      head.addEventListener("click", function () { card.classList.toggle("open"); });
      card.appendChild(head);
      card.appendChild(body);
      box.appendChild(card);
    });
    messages.appendChild(box);
    messages.scrollTop = messages.scrollHeight;
  }

  chatForm.addEventListener("submit", function (e) {
    e.preventDefault();
    var question = chatInput.value.trim();
    if (!question) return;
    chatInput.value = "";
    addMessage(question, "user");
    var thinking = addMessage("Thinking…", "bot");
    thinking.classList.add("typing");
    sendBtn.disabled = true;

    fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: question }),
    })
      .then(function (r) {
        return r.json().then(function (body) { return { ok: r.ok, body: body }; });
      })
      .then(function (res) {
        thinking.classList.remove("typing");
        if (!res.ok) {
          thinking.textContent = res.body.detail || "Something went wrong.";
          return;
        }
        thinking.textContent = res.body.answer;
        addSources(res.body.citations);
      })
      .catch(function () {
        thinking.classList.remove("typing");
        thinking.textContent = "Could not reach the server.";
      })
      .finally(function () { sendBtn.disabled = false; });
  });

  refreshDocuments();
})();
