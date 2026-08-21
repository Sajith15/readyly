(function () {
  "use strict";

  const messages = document.getElementById("messages");
  const composer = document.getElementById("composer");
  const input = document.getElementById("input");
  const send = document.getElementById("send");

  function escapeHtml(value) {
    const div = document.createElement("div");
    div.textContent = value;
    return div.innerHTML;
  }

  // Just enough markdown for the co-pilot's replies. Escaping happens first,
  // so nothing the model emits can inject markup.
  function render(text) {
    const lines = escapeHtml(text).split("\n");
    let html = "";
    let inList = false;

    for (const line of lines) {
      const bullet = line.match(/^\s*[-*]\s+(.*)$/);
      if (bullet) {
        if (!inList) {
          html += "<ul>";
          inList = true;
        }
        html += "<li>" + inline(bullet[1]) + "</li>";
        continue;
      }
      if (inList) {
        html += "</ul>";
        inList = false;
      }
      if (line.trim()) {
        html += "<p>" + inline(line) + "</p>";
      }
    }
    if (inList) {
      html += "</ul>";
    }
    return html || "<p></p>";
  }

  function inline(text) {
    return text
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
        '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
      .replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g,
        '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>');
  }

  function clearEmptyState() {
    const empty = messages.querySelector(".empty-state");
    if (empty) {
      empty.remove();
    }
  }

  function addMessage(role, text, options) {
    clearEmptyState();
    const wrapper = document.createElement("div");
    wrapper.className = "msg msg-" + role;

    const bubble = document.createElement("div");
    bubble.className = "bubble";

    if (role === "assistant") {
      bubble.innerHTML = render(text);
    } else {
      bubble.textContent = text;
    }
    wrapper.appendChild(bubble);

    const tools = options && options.tools;
    if (tools && tools.length) {
      const badges = document.createElement("div");
      badges.className = "tool-badges";
      const unique = [...new Set(tools)];
      badges.textContent = "via MCP: " + unique.join(", ");
      wrapper.appendChild(badges);
    }

    messages.appendChild(wrapper);
    messages.scrollTop = messages.scrollHeight;
    return wrapper;
  }

  function addPending() {
    clearEmptyState();
    const wrapper = document.createElement("div");
    wrapper.className = "msg msg-assistant";
    wrapper.innerHTML =
      '<div class="bubble typing"><span></span><span></span><span></span></div>';
    messages.appendChild(wrapper);
    messages.scrollTop = messages.scrollHeight;
    return wrapper;
  }

  async function submit(text) {
    addMessage("user", text);
    const pending = addPending();
    input.value = "";
    input.disabled = true;
    send.disabled = true;

    try {
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text }),
      });

      if (response.status === 401) {
        window.location.href = "/login";
        return;
      }

      const data = await response.json().catch(() => ({}));
      pending.remove();

      if (!response.ok) {
        addMessage("error", data.error || "Something went wrong. Please try again.");
        return;
      }
      addMessage("assistant", data.reply || "(no reply)", { tools: data.tools_used });
    } catch (err) {
      pending.remove();
      addMessage("error", "Could not reach the server. Check your connection.");
    } finally {
      input.disabled = false;
      send.disabled = false;
      input.focus();
    }
  }

  composer.addEventListener("submit", function (event) {
    event.preventDefault();
    const text = input.value.trim();
    if (text) {
      submit(text);
    }
  });

  document.addEventListener("click", function (event) {
    if (event.target.classList.contains("chip")) {
      submit(event.target.textContent.trim());
    }
  });

  input.focus();
})();
