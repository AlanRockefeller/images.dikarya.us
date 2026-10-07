/* Dikarya Images: progressive enhancement for search, galleries and ranking.
   Everything here is optional; pages work as plain HTML without it. */
(() => {
  "use strict";

  const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  /* Remote photos can disappear; show a placeholder instead of a broken image. */
  document.addEventListener("error", (event) => {
    const img = event.target;
    if (img instanceof HTMLImageElement) img.closest(".tile, .taxon-card__media")?.classList.add("is-broken");
  }, true);

  /* Filter dropdowns apply immediately. */
  $$("form[data-autosubmit] select, form[data-autosubmit] input[type=checkbox]").forEach((control) => {
    control.addEventListener("change", () => control.form.requestSubmit());
  });

  /* ---------- Typeahead search ---------- */

  function typeahead(input) {
    const pickMode = input.dataset.typeahead === "pick";
    const list = document.createElement("ul");
    list.className = "typeahead";
    list.id = `${input.id || "ta"}-${Math.random().toString(36).slice(2, 8)}-list`;
    list.setAttribute("role", "listbox");
    list.hidden = true;
    input.after(list);
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-expanded", "false");
    input.setAttribute("aria-controls", list.id);
    let results = [];
    let active = -1;
    let timer = null;
    let controller = null;

    const close = () => {
      list.hidden = true;
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
      active = -1;
    };

    const highlight = (index) => {
      active = index;
      $$("li", list).forEach((li, i) => li.setAttribute("aria-selected", String(i === index)));
      if (index >= 0) input.setAttribute("aria-activedescendant", `${list.id}-${index}`);
    };

    const choose = (index) => {
      const result = results[index];
      if (!result) return;
      if (pickMode) {
        input.value = result.id;
        input.title = result.name;
        let note = input.parentElement.querySelector(".pick-note");
        if (!note) {
          note = document.createElement("small");
          note.className = "pick-note muted";
          input.after(note);
        }
        note.textContent = `${result.name} (${result.rank})`;
        close();
      } else {
        window.location.href = result.url;
      }
    };

    const render = () => {
      list.replaceChildren(...results.map((result, index) => {
        const li = document.createElement("li");
        li.id = `${list.id}-${index}`;
        li.setAttribute("role", "option");
        const link = document.createElement(pickMode ? "button" : "a");
        if (pickMode) link.type = "button"; else link.href = result.url;
        link.tabIndex = -1;
        const media = document.createElement(result.thumb ? "img" : "span");
        if (result.thumb) { media.src = result.thumb; media.alt = ""; } else { media.className = "ph"; }
        const text = document.createElement("span");
        const name = document.createElement(result.italic ? "i" : "span");
        name.className = "t-name";
        name.textContent = result.name;
        const meta = document.createElement("span");
        meta.className = "t-meta";
        meta.textContent = [result.rank, result.common_name, result.matched && `matches “${result.matched}”`]
          .filter(Boolean).join(" · ");
        text.append(name, meta);
        const count = document.createElement("span");
        count.className = "t-count";
        count.textContent = pickMode ? `iNat ${result.id}` : `${result.photos.toLocaleString()} photos`;
        link.append(media, text, count);
        link.addEventListener("mousedown", (event) => { event.preventDefault(); choose(index); });
        li.append(link);
        return li;
      }));
      list.hidden = results.length === 0;
      input.setAttribute("aria-expanded", String(!list.hidden));
      highlight(-1);
    };

    const search = async () => {
      const q = input.value.trim();
      if (q.length < 2 || (pickMode && /^\d+$/.test(q))) { results = []; render(); return; }
      controller?.abort();
      controller = new AbortController();
      try {
        const scope = pickMode ? "&scope=all" : "";
        const response = await fetch(`/api/search?q=${encodeURIComponent(q)}${scope}`, { signal: controller.signal });
        if (!response.ok) return;
        results = (await response.json()).results;
        render();
      } catch (error) {
        if (error.name !== "AbortError") results = [];
      }
    };

    input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(search, 160); });
    input.addEventListener("keydown", (event) => {
      if (list.hidden) return;
      if (event.key === "ArrowDown") { event.preventDefault(); highlight(Math.min(active + 1, results.length - 1)); }
      else if (event.key === "ArrowUp") { event.preventDefault(); highlight(Math.max(active - 1, -1)); }
      else if (event.key === "Enter" && active >= 0) { event.preventDefault(); choose(active); }
      else if (event.key === "Escape") { close(); }
    });
    input.addEventListener("blur", () => setTimeout(close, 120));
  }

  $$("input[data-typeahead]").forEach(typeahead);

  /* ---------- Taxon gallery ---------- */

  const gallery = document.getElementById("gallery");
  if (!gallery) return;
  const tray = document.getElementById("tray");
  const trayList = tray && $(".tray__list", tray);
  const trayStatus = tray && $(".tray__status", tray);
  const viewer = document.getElementById("viewer");

  // Details we need to draw ranked photos even when their tile isn't loaded.
  const info = new Map();
  let ranking = trayList ? $$(".tray__item", trayList).map((li) => Number(li.dataset.id)) : [];
  if (trayList) $$(".tray__item", trayList).forEach((li) => info.set(Number(li.dataset.id), { thumb: li.dataset.thumb, credit: "" }));

  const tileFor = (id) => gallery.querySelector(`.tile[data-id="${id}"]`);
  const describe = (id) => {
    const tile = tileFor(id);
    if (tile) info.set(id, { thumb: tile.dataset.thumb, credit: tile.dataset.credit });
    return info.get(id) || { thumb: "", credit: "" };
  };

  function setStatus(text, kind = "") {
    if (!trayStatus) return;
    trayStatus.textContent = text;
    trayStatus.className = `tray__status${kind ? ` is-${kind}` : ""}`;
  }

  function paintTiles(root = gallery) {
    const positions = new Map(ranking.map((id, index) => [id, index + 1]));
    $$(".tile", root).forEach((tile) => {
      const rank = positions.get(Number(tile.dataset.id));
      tile.classList.toggle("is-ranked", Boolean(rank));
      $(".tile__rank", tile).textContent = rank || "";
      const button = $("button.tile__select", tile);
      if (button) {
        button.setAttribute("aria-pressed", String(Boolean(rank)));
        $(".visually-hidden", button).textContent = rank
          ? `Ranked ${rank}. Remove from your ranking` : "Add to your ranking";
      }
    });
  }

  let activeItem = null; // tray item showing its move/remove controls (touch and keyboard)

  function paintTray() {
    if (!trayList) return;
    const focusedId = document.activeElement?.closest?.(".tray__item")?.dataset.id;
    trayList.replaceChildren(...ranking.map((id, index) => {
      const { thumb, credit } = describe(id);
      const li = document.createElement("li");
      li.className = "tray__item";
      li.dataset.id = id;
      li.tabIndex = 0;
      li.setAttribute("aria-label", `Rank ${index + 1}${credit ? `, photo by ${credit}` : ""}. ` +
        "Alt plus arrow keys move it, Delete removes it.");
      li.innerHTML = '<span class="tray__num"></span><img alt="">' +
        '<button class="tray__remove" type="button" tabindex="-1" aria-label="Remove">×</button>' +
        '<span class="tray__moves"><button type="button" tabindex="-1" data-move="-1" aria-label="Move earlier">‹</button>' +
        '<button type="button" tabindex="-1" data-move="1" aria-label="Move later">›</button></span>';
      $(".tray__num", li).textContent = index + 1;
      $("img", li).src = thumb;
      if (String(id) === activeItem) li.classList.add("is-active");
      return li;
    }));
    if (focusedId) trayList.querySelector(`[data-id="${focusedId}"]`)?.focus();
  }

  function paint() {
    paintTiles();
    paintTray();
    if (viewer?.open) fillViewer();
  }

  /* Saving: the whole ordered list is sent; only one request is in flight and
     the latest state always wins. */
  let saving = false;
  let dirty = false;

  function save() {
    dirty = true;
    setStatus("Saving…");
    if (!saving) flush();
  }

  async function flush() {
    saving = true;
    let delay = 1000;
    while (dirty) {
      dirty = false;
      const sent = ranking.slice();
      try {
        const response = await fetch(tray.dataset.api, {
          method: "PUT",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json", Accept: "application/json", "X-CSRF-Token": csrfToken },
          body: JSON.stringify({ image_ids: sent }),
        });
        if (response.status === 401) {
          window.location.href = `/login?next=${encodeURIComponent(location.pathname)}`;
          return;
        }
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const saved = (await response.json()).image_ids;
        if (!dirty && saved.join() !== ranking.join()) {
          ranking = saved; // the server dropped photos that are no longer available
          paint();
        }
        setStatus("Saved", "ok");
        delay = 1000;
      } catch (error) {
        dirty = true;
        setStatus("Couldn’t save. Retrying…", "error");
        await sleep(delay);
        delay = Math.min(delay * 2, 30000);
      }
    }
    saving = false;
  }

  window.addEventListener("beforeunload", (event) => {
    if (dirty || saving) event.preventDefault();
  });

  function toggle(id) {
    const index = ranking.indexOf(id);
    if (index >= 0) {
      ranking.splice(index, 1);
    } else {
      describe(id);
      ranking.push(id);
    }
    paint();
    save();
  }

  function move(id, offset) {
    const from = ranking.indexOf(id);
    const to = from + offset;
    if (from < 0 || to < 0 || to >= ranking.length) return;
    ranking.splice(to, 0, ranking.splice(from, 1)[0]);
    paint();
    save();
  }

  gallery.addEventListener("click", (event) => {
    const tile = event.target.closest(".tile");
    if (!tile) return;
    if (event.target.closest(".tile__zoom")) {
      openViewer(tile);
    } else if (event.target.closest(".tile__select")) {
      event.preventDefault();
      if (tray) toggle(Number(tile.dataset.id));
      else openViewer(tile);
    }
  });

  /* Tray: mouse drag to reorder; tap or focus an item for move/remove buttons. */
  if (trayList) {
    let drag = null;

    trayList.addEventListener("pointerdown", (event) => {
      const item = event.target.closest(".tray__item");
      if (!item || event.target.closest("button") || event.button !== 0 || event.pointerType === "touch") return;
      drag = { item, id: Number(item.dataset.id), x: event.clientX, moved: false, pointer: event.pointerId };
    });

    trayList.addEventListener("pointermove", (event) => {
      if (!drag || event.pointerId !== drag.pointer) return;
      if (!drag.moved) {
        if (Math.abs(event.clientX - drag.x) < 5) return;
        drag.moved = true;
        drag.item.classList.add("is-dragging");
        trayList.setPointerCapture(event.pointerId);
      }
      const others = $$(".tray__item", trayList).filter((li) => li !== drag.item);
      const before = others.find((li) => {
        const box = li.getBoundingClientRect();
        return event.clientX < box.left + box.width / 2;
      });
      if (before) { if (drag.item.nextElementSibling !== before) trayList.insertBefore(drag.item, before); }
      else if (trayList.lastElementChild !== drag.item) trayList.append(drag.item);
      const box = trayList.getBoundingClientRect();
      if (event.clientX > box.right - 40) trayList.scrollLeft += 12;
      if (event.clientX < box.left + 40) trayList.scrollLeft -= 12;
    });

    const endDrag = () => {
      if (!drag) return;
      const { moved, item } = drag;
      drag = null;
      if (!moved) return;
      item.classList.remove("is-dragging");
      const order = $$(".tray__item", trayList).map((li) => Number(li.dataset.id));
      if (order.join() !== ranking.join()) { ranking = order; paint(); save(); }
    };
    trayList.addEventListener("pointerup", endDrag);
    trayList.addEventListener("pointercancel", endDrag);

    trayList.addEventListener("click", (event) => {
      const item = event.target.closest(".tray__item");
      if (!item) return;
      const id = Number(item.dataset.id);
      const action = event.target.closest("button");
      if (action?.classList.contains("tray__remove")) { activeItem = null; toggle(id); return; }
      if (action?.dataset.move) { move(id, Number(action.dataset.move)); return; }
      activeItem = activeItem === item.dataset.id ? null : item.dataset.id;
      $$(".tray__item", trayList).forEach((li) => li.classList.toggle("is-active", li.dataset.id === activeItem));
      const tile = tileFor(id);
      if (tile && !activeItem) openViewer(tile);
    });

    trayList.addEventListener("keydown", (event) => {
      const item = event.target.closest(".tray__item");
      if (!item) return;
      const id = Number(item.dataset.id);
      if ((event.altKey || event.shiftKey) && (event.key === "ArrowLeft" || event.key === "ArrowRight")) {
        event.preventDefault();
        move(id, event.key === "ArrowLeft" ? -1 : 1);
        setStatus(`Moved to #${ranking.indexOf(id) + 1}. Saving…`);
      } else if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
        event.preventDefault();
        (event.key === "ArrowLeft" ? item.previousElementSibling : item.nextElementSibling)?.focus();
      } else if (event.key === "Delete" || event.key === "Backspace") {
        event.preventDefault();
        const neighbor = item.nextElementSibling || item.previousElementSibling;
        toggle(id);
        if (neighbor) trayList.querySelector(`[data-id="${neighbor.dataset.id}"]`)?.focus();
      } else if (event.key === "Enter") {
        const tile = tileFor(id);
        if (tile) openViewer(tile);
      }
    });

    paintTray();
  }

  /* ---------- Infinite scroll ---------- */

  const loader = new IntersectionObserver((entries) => {
    entries.forEach((entry) => { if (entry.isIntersecting) loadMore(entry.target); });
  }, { rootMargin: "1000px 0px" });

  async function loadMore(more) {
    if (!more || more.classList.contains("is-loading")) return null;
    loader.unobserve(more);
    more.classList.add("is-loading");
    try {
      const response = await fetch(more.dataset.next, { headers: { Accept: "text/html" } });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const template = document.createElement("template");
      template.innerHTML = await response.text();
      const first = template.content.querySelector(".tile");
      more.replaceWith(template.content);
      paintTiles();
      watchMore();
      return first ? tileFor(first.dataset.id) : null;
    } catch (error) {
      more.classList.remove("is-loading"); // the link still works as a fallback
      return null;
    }
  }

  function watchMore() {
    const more = $(".gallery__more", gallery);
    if (more) loader.observe(more);
  }

  gallery.addEventListener("click", (event) => {
    const link = event.target.closest(".gallery__more a");
    if (link) { event.preventDefault(); loadMore(link.parentElement); }
  });
  watchMore();

  /* ---------- Photo viewer ---------- */

  let current = null;

  function fillViewer() {
    if (!current) return;
    const data = current.dataset;
    const id = Number(data.id);
    const img = $(".viewer__img", viewer);
    img.src = data.large;
    img.alt = $("img", current)?.alt || "";
    $(".viewer__taxon-row", viewer).hidden = !data.taxon;
    $(".viewer__taxon", viewer).textContent = data.taxon;
    $(".viewer__credit", viewer).textContent = data.credit;
    const license = $(".viewer__license", viewer);
    license.replaceChildren();
    if (data.licenseUrl) {
      const link = document.createElement("a");
      link.href = data.licenseUrl;
      link.rel = "license noopener";
      link.textContent = data.license;
      license.append(link);
    } else {
      license.textContent = data.license;
    }
    $(".viewer__observed", viewer).textContent = data.observed
      ? new Date(`${data.observed}T12:00:00`).toLocaleDateString(undefined, { year: "numeric", month: "long", day: "numeric" })
      : "Unknown";
    $(".viewer__its", viewer).textContent = data.its ? `${Number(data.its).toLocaleString()} bases` : "Recorded at source";
    $(".viewer__faves-row", viewer).hidden = data.faves === "";
    $(".viewer__faves", viewer).textContent = data.faves;
    const source = $(".viewer__source", viewer);
    source.href = data.obs;
    source.textContent = `View observation on ${data.source} ↗`;
    const state = $(".viewer__state", viewer);
    const toggleButton = $(".viewer__toggle", viewer);
    if (toggleButton) {
      const rank = ranking.indexOf(id) + 1;
      state.textContent = rank ? `Your #${rank}` : "Not in your ranking";
      state.classList.toggle("is-ranked", rank > 0);
      toggleButton.textContent = rank ? "Remove from my ranking" : `Add as #${ranking.length + 1}`;
      toggleButton.classList.toggle("is-ranked", rank > 0);
    }
    $(".viewer__nav--prev", viewer).hidden = !current.previousElementSibling?.matches(".tile");
    $(".viewer__nav--next", viewer).hidden = !current.nextElementSibling;
    // Warm the cache for the neighbours.
    [current.previousElementSibling, current.nextElementSibling].forEach((tile) => {
      if (tile?.dataset?.large) new Image().src = tile.dataset.large;
    });
  }

  function openViewer(tile) {
    if (!viewer) return;
    current = tile;
    fillViewer();
    if (!viewer.open) viewer.showModal();
  }

  async function step(offset) {
    if (!current) return;
    let next = offset < 0 ? current.previousElementSibling : current.nextElementSibling;
    if (next?.matches(".gallery__more")) next = await loadMore(next);
    if (next?.matches(".tile")) { current = next; fillViewer(); }
  }

  if (viewer) {
    viewer.addEventListener("click", (event) => {
      if (event.target === viewer || event.target.closest(".viewer__close")) viewer.close();
      else if (event.target.closest(".viewer__nav--prev")) step(-1);
      else if (event.target.closest(".viewer__nav--next")) step(1);
      else if (event.target.closest(".viewer__toggle")) toggle(Number(current.dataset.id));
    });
    viewer.addEventListener("keydown", (event) => {
      if (event.key === "ArrowLeft") { event.preventDefault(); step(-1); }
      else if (event.key === "ArrowRight") { event.preventDefault(); step(1); }
      else if ((event.key === "r" || event.key === "R") && tray && !event.metaKey && !event.ctrlKey) toggle(Number(current.dataset.id));
    });
    viewer.addEventListener("close", () => {
      current?.querySelector(".tile__select, .tile__zoom")?.focus({ preventScroll: true });
      current?.scrollIntoView({ block: "nearest" });
    });
  }

  /* Keep the page's bottom padding in step with the tray height. */
  if (tray) {
    const sync = () => document.documentElement.style.setProperty("--tray-h", `${tray.offsetHeight}px`);
    new ResizeObserver(sync).observe(tray);
    sync();
  }
  paintTiles();
})();
