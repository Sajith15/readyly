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

  function atBottom() {
    return messages.scrollHeight - messages.scrollTop - messages.clientHeight < 120;
  }

  function scrollDown(force) {
    if (force || atBottom()) {
      messages.scrollTop = messages.scrollHeight;
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
      badges.textContent = "via MCP: " + [...new Set(tools)].join(", ");
      wrapper.appendChild(badges);
    }

    messages.appendChild(wrapper);
    scrollDown(true);
    return wrapper;
  }

  // --- how each tool is described in the timeline ---------------------------

  const TOOLS = {
    add_bookmark: { running: "Saving bookmark", done: "Saved bookmark" },
    list_bookmarks: { running: "Reading your bookmarks", done: "Read your bookmarks" },
    search_bookmarks: { running: "Searching bookmarks", done: "Searched bookmarks" },
    delete_bookmark: { running: "Deleting bookmark", done: "Deleted bookmark" },
  };

  const STAGES = {
    connecting: "Connecting to your bookmark tools",
    thinking: "Thinking",
    wrapping_up: "Wrapping up",
  };

  function toolLabel(name, phase) {
    const entry = TOOLS[name];
    if (entry) {
      return entry[phase];
    }
    return (phase === "running" ? "Calling " : "Called ") + name;
  }

  // Turn a tool's arguments into a few readable chips.
  function argChips(args) {
    const chips = [];
    Object.keys(args || {}).forEach(function (key) {
      let value = args[key];
      if (value === null || value === undefined || value === "") return;
      if (Array.isArray(value)) {
        if (!value.length) return;
        value = value.join(", ");
      }
      value = String(value);
      if (value.length > 60) value = value.slice(0, 57) + "\u2026";
      chips.push(key + ": " + value);
    });
    return chips;
  }

  // A short human summary of what a tool returned.
  function resultSummary(name, result) {
    if (!result || typeof result !== "object") return "";
    if (result.error) return String(result.error);

    if (name === "add_bookmark" && result.saved) {
      return "\u201c" + (result.saved.title || result.saved.url) + "\u201d";
    }
    if (name === "delete_bookmark") {
      return result.deleted ? "removed" : "nothing matched";
    }
    if (typeof result.count === "number") {
      return result.count === 1 ? "1 bookmark" : result.count + " bookmarks";
    }
    return "";
  }

  // --- the activity timeline ------------------------------------------------

  function createActivity() {
    clearEmptyState();

    const wrapper = document.createElement("div");
    wrapper.className = "msg msg-assistant";

    const card = document.createElement("div");
    card.className = "activity is-running";

    const header = document.createElement("button");
    header.type = "button";
    header.className = "activity-header";
    header.innerHTML =
      '<span class="activity-spinner" aria-hidden="true"></span>' +
      '<span class="activity-title">Working\u2026</span>' +
      '<span class="activity-chevron" aria-hidden="true"></span>';
    card.appendChild(header);

    const body = document.createElement("div");
    body.className = "activity-body";
    card.appendChild(body);

    wrapper.appendChild(card);
    messages.appendChild(wrapper);
    scrollDown(true);

    const title = header.querySelector(".activity-title");
    const startedAt = performance.now();
    let current = null;

    header.addEventListener("click", function () {
      card.classList.toggle("is-collapsed");
    });

    function settleCurrent(detail) {
      if (!current) return;
      current.row.classList.remove("is-running");
      current.row.classList.add("is-done");
      if (detail) {
        current.label.textContent = detail;
      }
      if (current.startedAt) {
        const seconds = (performance.now() - current.startedAt) / 1000;
        current.timing.textContent = seconds.toFixed(1) + "s";
      }
      current = null;
    }

    function addRow(text) {
      const row = document.createElement("div");
      row.className = "activity-row is-running";

      const mark = document.createElement("span");
      mark.className = "activity-mark";
      row.appendChild(mark);

      const label = document.createElement("span");
      label.className = "activity-label";
      label.textContent = text;
      row.appendChild(label);

      const timing = document.createElement("span");
      timing.className = "activity-timing";
      row.appendChild(timing);

      body.appendChild(row);
      scrollDown(false);
      return { row: row, label: label, timing: timing, startedAt: performance.now() };
    }

    return {
      element: wrapper,

      step: function (stage) {
        settleCurrent();
        title.textContent = (STAGES[stage] || "Working") + "\u2026";
        current = addRow(STAGES[stage] || stage);
      },

      toolCall: function (name, args) {
        settleCurrent();
        title.textContent = toolLabel(name, "running") + "\u2026";
        current = addRow(toolLabel(name, "running"));
        current.toolName = name;

        const chips = argChips(args);
        if (chips.length) {
          const list = document.createElement("div");
          list.className = "activity-args";
          chips.forEach(function (chip) {
            const span = document.createElement("span");
            span.className = "activity-chip";
            span.textContent = chip;
            list.appendChild(span);
          });
          current.row.appendChild(list);
          scrollDown(false);
        }
      },

      toolResult: function (name, ok, result) {
        const summary = resultSummary(name, result);
        const label = toolLabel(name, "done") + (summary ? " \u2014 " + summary : "");
        if (current) {
          current.row.classList.toggle("is-failed", !ok);
        }
        settleCurrent(label);
      },

      fail: function () {
        settleCurrent();
        card.classList.remove("is-running");
        card.classList.add("is-collapsed");
        title.textContent = "Stopped";
      },

      finish: function (toolsUsed) {
        settleCurrent();
        card.classList.remove("is-running");
        card.classList.add("is-collapsed");
        const seconds = ((performance.now() - startedAt) / 1000).toFixed(1);
        const count = (toolsUsed || []).length;
        const tools = count
          ? " \u00b7 " + count + (count === 1 ? " tool" : " tools")
          : "";
        title.textContent = "Worked for " + seconds + "s" + tools;
      },
    };
  }

  // --- progressive reveal of the final answer -------------------------------

  // The reply arrives complete: the model's own output is not streamed token by
  // token, so this is presentation only. It keeps a long answer from landing as
  // one block after a visible pause.
  function reveal(bubble, text) {
    return new Promise(function (resolve) {
      const total = text.length;
      // Long answers should not take proportionally longer to appear.
      const perFrame = Math.max(2, Math.ceil(total / 90));
      let shown = 0;

      function tick() {
        shown = Math.min(total, shown + perFrame);
        bubble.textContent = text.slice(0, shown);
        scrollDown(false);
        if (shown < total) {
          requestAnimationFrame(tick);
          return;
        }
        bubble.innerHTML = render(text);
        scrollDown(false);
        resolve();
      }
      requestAnimationFrame(tick);
    });
  }

  // --- the turn -------------------------------------------------------------

  async function submit(text) {
    addMessage("user", text);
    input.value = "";
    input.disabled = true;
    send.disabled = true;

    const activity = createActivity();
    let finished = false;

    function handle(event) {
      if (event.type === "step") {
        activity.step(event.stage);
      } else if (event.type === "tool_call") {
        activity.toolCall(event.name, event.arguments);
      } else if (event.type === "tool_result") {
        activity.toolResult(event.name, event.ok, event.result);
      } else if (event.type === "done") {
        finished = true;
        activity.finish(event.tools_used);
        const wrapper = addMessage("assistant", "", { tools: event.tools_used });
        const bubble = wrapper.querySelector(".bubble");
        bubble.textContent = "";
        reveal(bubble, event.reply || "(no reply)");
      } else if (event.type === "error") {
        finished = true;
        activity.fail();
        addMessage("error", event.message || "Something went wrong.");
      }
    }

    try {
      const response = await fetch("/api/chat/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text }),
      });

      if (response.status === 401) {
        window.location.href = "/login";
        return;
      }
      if (!response.ok || !response.body) {
        activity.fail();
        addMessage("error", "Something went wrong. Please try again.");
        return;
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const chunk = await reader.read();
        if (chunk.done) break;
        buffer += decoder.decode(chunk.value, { stream: true });

        let split;
        while ((split = buffer.indexOf("\n\n")) !== -1) {
          const frame = buffer.slice(0, split);
          buffer = buffer.slice(split + 2);
          const line = frame.split("\n").find(function (l) {
            return l.indexOf("data: ") === 0;
          });
          if (!line) continue;
          try {
            handle(JSON.parse(line.slice(6)));
          } catch (err) {
            /* ignore a partial or malformed frame */
          }
        }
      }

      if (!finished) {
        activity.fail();
        addMessage("error", "The connection ended before a reply arrived.");
      }
    } catch (err) {
      activity.fail();
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
