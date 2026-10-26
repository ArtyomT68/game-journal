// Окно добавления игры: живой поиск по каталогу.
const modal = document.getElementById("add-modal");
const searchInput = document.getElementById("add-search");
const resultsBox = document.getElementById("add-results");
const addForm = document.getElementById("add-form");
let searchTimer = null;
let searchSeq = 0;

const addHeading = document.getElementById("add-heading");
const addManual = document.getElementById("add-manual");
// Кнопка в шапке знает, куда добавлять на текущей странице: библиотека, «Сейчас прохожу» или хотелки
const headerAdd = document.querySelector(".top [data-open-add]");
const STATUSES = ["completed", "playing", "wishlist"];
const IN_LIBRARY = { completed: "✓ пройдена", playing: "▶ прохожу", wishlist: "★ в хотелках" };

// status: "completed" — пройденная игра, "playing" — начатая, "wishlist" — хочу пройти
function openAdd(status) {
  status = STATUSES.includes(status) ? status : "completed";
  addForm.elements.namedItem("status").value = status;
  addHeading.textContent = addHeading.dataset[status];
  addManual.href = `/games/new?status=${status}`;
  modal.hidden = false;
  searchInput.focus();
  searchInput.select();
}
function closeAdd() { modal.hidden = true; }

document.querySelectorAll("[data-open-add]").forEach(b =>
  b.addEventListener("click", () => openAdd(b.dataset.openAdd)));
document.querySelectorAll("[data-close-add]").forEach(b => b.addEventListener("click", closeAdd));
modal.addEventListener("click", e => { if (e.target === modal) closeAdd(); });
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && !modal.hidden) closeAdd();
  // «/» открывает поиск, если фокус не в поле ввода
  if (e.key === "/" && modal.hidden && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) {
    e.preventDefault();
    openAdd(headerAdd.dataset.openAdd);
  }
});

function status(text) {
  resultsBox.innerHTML = "";
  const p = document.createElement("div");
  p.className = "status";
  p.textContent = text;
  resultsBox.append(p);
}

function renderResults(data) {
  resultsBox.innerHTML = "";
  if (!data.results.length) {
    status(data.errors.length ? data.errors.join(", ") : "Ничего не найдено");
    return;
  }
  for (const r of data.results) {
    const row = document.createElement("div");
    row.className = "result";

    const img = document.createElement("img");
    img.loading = "lazy";
    img.src = r.cover || "";
    img.onerror = () => {
      if (r.thumb_fallback && img.src !== r.thumb_fallback) img.src = r.thumb_fallback;
      else img.style.visibility = "hidden";
    };

    const t = document.createElement("div");
    t.className = "t";
    const b = document.createElement("b");
    b.textContent = r.title;
    const s = document.createElement("small");
    s.textContent = r.subtitle;
    t.append(b, s);

    let action;
    if (r.in_library) {
      action = document.createElement("span");
      action.className = "tag";
      action.textContent = IN_LIBRARY[r.in_library] || "✓ уже в библиотеке";
    } else {
      action = document.createElement("button");
      action.className = "btn small";
      action.type = "button";
      action.textContent = "Добавить";
      action.addEventListener("click", () => {
        action.disabled = true;
        action.textContent = "Добавляю…";
        // form.id / form.title — это атрибуты самой формы, поэтому через elements
        addForm.elements.namedItem("source").value = r.source;
        addForm.elements.namedItem("id").value = r.id;
        addForm.elements.namedItem("title").value = r.title;
        addForm.submit();
      });
    }
    row.append(img, t, action);
    resultsBox.append(row);
  }
}

searchInput.addEventListener("input", () => {
  clearTimeout(searchTimer);
  const q = searchInput.value.trim();
  if (q.length < 2) { resultsBox.innerHTML = ""; return; }
  searchTimer = setTimeout(async () => {
    const seq = ++searchSeq;
    status("Ищу…");
    try {
      const resp = await fetch(`/api/search?q=${encodeURIComponent(q)}`);
      const data = await resp.json();
      if (seq === searchSeq) renderResults(data);
    } catch {
      if (seq === searchSeq) status("Ошибка поиска — проверь подключение к интернету");
    }
  }, 300);
});

// Подтверждение удаления
document.querySelectorAll("form[data-confirm]").forEach(f =>
  f.addEventListener("submit", e => { if (!confirm(f.dataset.confirm)) e.preventDefault(); }));

// Блокировка кнопки на время долгих запросов (синхронизация, импорт)
document.querySelectorAll("form[data-busy]").forEach(f =>
  f.addEventListener("submit", () => {
    const btn = f.querySelector("button[type=submit], button:not([type])");
    if (btn) { btn.disabled = true; btn.textContent = f.dataset.busy; }
  }));

// Кнопки −/+ у количества прохождений
document.querySelectorAll("[data-step]").forEach(b => b.addEventListener("click", () => {
  const input = b.parentElement.querySelector("input");
  input.value = Math.max(0, (parseInt(input.value, 10) || 0) + Number(b.dataset.step));
}));

// «из Steam» — подставить процент достижений
document.querySelectorAll("[data-fill]").forEach(b => b.addEventListener("click", () => {
  document.querySelector(b.dataset.fill).value = b.dataset.value;
}));

// Оценки 1–5 для 100% достижений: повторный клик по выбранной оценке сбрасывает её
document.querySelectorAll("[data-rating]").forEach(box => {
  const input = box.querySelector("input");
  const paint = () => box.querySelectorAll("button").forEach(b =>
    b.classList.toggle("on", Number(b.dataset.v) <= Number(input.value || 0)));
  box.querySelectorAll("button").forEach(b => b.addEventListener("click", () => {
    input.value = input.value === b.dataset.v ? "" : b.dataset.v;
    paint();
  }));
  paint();
});

// Кнопки с долгим запросом внутри обычной формы (например, «Обновить оценку»)
document.querySelectorAll("[data-busy-btn]").forEach(b => b.addEventListener("click", () => {
  setTimeout(() => { b.disabled = true; b.textContent = b.dataset.busyBtn; });
}));

// Карточка игры из поиска: оценка 100% подгружается отдельно (HowLongToBeat отвечает несколько секунд)
const estimateBox = document.getElementById("estimate");
if (estimateBox) {
  const field = name => estimateBox.querySelector(`[data-field="${name}"]`);
  const dots = (value, kind) => {
    const span = document.createElement("span");
    span.className = `dots ${kind}`;
    for (let i = 1; i <= 5; i++) {
      const dot = document.createElement("i");
      if (value && i <= value) dot.className = "on";
      span.append(dot);
    }
    const label = document.createElement("b");
    label.textContent = value ? `${value}/5` : "нет данных";
    span.append(label);
    return span;
  };
  const params = new URLSearchParams({
    title: estimateBox.dataset.title, year: estimateBox.dataset.year, appid: estimateBox.dataset.appid,
  });
  fetch(`/api/estimate?${params}`)
    .then(r => r.json())
    .then(e => {
      field("main").textContent = e.hltb_main ? `~${Math.round(e.hltb_main)} ч` : "—";
      field("fun").replaceChildren(dots(e.auto_fun, "fun"));
      field("grind").replaceChildren(dots(e.auto_grind, "grind"));
      field("full").textContent = e.hltb_100 ? `На 100% достижений уходит ~${Math.round(e.hltb_100)} ч.` : "";
      for (const reason of (e.auto_reasons || "").split("\n").filter(Boolean)) {
        const li = document.createElement("li");
        li.textContent = reason;
        field("reasons").append(li);
      }
      if (e.verdict) {
        const v = field("verdict");
        v.textContent = e.verdict.text;
        v.className = `verdict ${e.verdict.cls}`;
        v.hidden = false;
      }
      field("status").textContent = e.hltb_id
        ? "Источники: HowLongToBeat и достижения Steam."
        : "На HowLongToBeat этой игры нет или по ней мало статистики.";
    })
    .catch(() => { field("status").textContent = "Не удалось загрузить данные HowLongToBeat."; });
}

// Подсказки при вводе в поиске: похожие игры из Steam, клик — карточка игры
const SUGGEST_STATUS = { completed: "✓ пройдена", playing: "▶ прохожу", wishlist: "★ в хотелках" };
document.querySelectorAll("[data-suggest]").forEach(input => {
  const box = document.createElement("div");
  box.className = "suggest";
  box.hidden = true;
  input.parentElement.classList.add("suggest-host");
  input.parentElement.append(box);
  let timer = null, seq = 0, active = -1;

  const items = () => [...box.querySelectorAll("a")];
  const highlight = i => {
    active = i;
    items().forEach((a, k) => a.classList.toggle("on", k === i));
  };
  const close = () => { box.hidden = true; active = -1; };

  input.addEventListener("input", () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < 2) { close(); return; }
    timer = setTimeout(async () => {
      const my = ++seq;
      let data;
      try {
        data = await (await fetch(`/api/suggest?q=${encodeURIComponent(q)}`)).json();
      } catch { return; }
      if (my !== seq) return;
      box.replaceChildren();
      for (const r of data.results) {
        const a = document.createElement("a");
        a.href = r.url;
        const img = document.createElement("img");
        img.src = r.thumb || "";
        img.alt = "";
        img.onerror = () => { img.style.visibility = "hidden"; };
        const t = document.createElement("span");
        t.className = "t";
        const b = document.createElement("b");
        b.textContent = r.title;
        const s = document.createElement("small");
        s.textContent = r.in_library ? `${r.subtitle} · ${SUGGEST_STATUS[r.in_library]}` : r.subtitle;
        t.append(b, s);
        a.append(img, t);
        box.append(a);
      }
      const all = document.createElement("a");
      all.className = "all";
      all.href = `/explore?q=${encodeURIComponent(q)}`;
      all.textContent = `Все результаты по «${q}» →`;
      box.append(all);
      active = -1;
      box.hidden = false;
    }, 250);
  });

  // стрелки ↑↓ и Enter по подсказкам, Esc — закрыть
  input.addEventListener("keydown", e => {
    if (box.hidden) return;
    const list = items();
    if (e.key === "ArrowDown") { e.preventDefault(); highlight((active + 1) % list.length); }
    else if (e.key === "ArrowUp") { e.preventDefault(); highlight((active - 1 + list.length) % list.length); }
    else if (e.key === "Enter" && active >= 0) { e.preventDefault(); location.href = list[active].href; }
    else if (e.key === "Escape") { close(); }
  });
  input.addEventListener("blur", () => setTimeout(close, 150));
});

// Кнопка «Фильтры» в поиске
document.querySelectorAll("[data-toggle-filters]").forEach(b => b.addEventListener("click", () => {
  const panel = b.closest("form").querySelector(".filters");
  panel.hidden = !panel.hidden;
}));

// Поиск по списку на странице (вкладка «Импорт»): фильтрует строки по названию на лету
document.querySelectorAll("[data-filter-list]").forEach(input => {
  const rows = [...document.querySelectorAll(`${input.dataset.filterList} [data-name]`)];
  const count = document.querySelector("[data-filter-count]");
  const empty = document.querySelector("[data-filter-empty]");
  input.addEventListener("input", () => {
    const words = input.value.toLowerCase().split(/\s+/).filter(Boolean);
    let shown = 0;
    for (const row of rows) {
      const ok = words.every(w => row.dataset.name.includes(w));
      row.hidden = !ok;
      shown += ok;
    }
    if (count) count.textContent = words.length ? `найдено: ${shown}` : "";
    if (empty) empty.hidden = shown > 0;
  });
});

// Вкладка «Импорт»: фильтры как в поиске — по названию, жанру, году, отзывам, популярности
const importForm = document.getElementById("import-filters");
if (importForm) {
  const list = document.querySelector(".import-list");
  const rows = [...list.querySelectorAll("[data-name]")];
  const count = document.querySelector("[data-filter-count]");
  const empty = document.querySelector("[data-filter-empty]");
  let touched = false;

  const num = v => (v === "" || v == null ? null : Number(v));
  const sorters = {
    playtime: (a, b) => b.dataset.playtime - a.dataset.playtime,
    name: (a, b) => a.dataset.name.localeCompare(b.dataset.name),
    new: (a, b) => (num(b.dataset.year) || 0) - (num(a.dataset.year) || 0),
    reviews: (a, b) => (num(b.dataset.rating) || 0) - (num(a.dataset.rating) || 0),
  };

  function apply() {
    const f = Object.fromEntries(new FormData(importForm));
    const words = (f.q || "").toLowerCase().split(/\s+/).filter(Boolean);
    const tags = [f.tag, f.tag2].filter(Boolean);
    const yFrom = num(f.year_from), yTo = num(f.year_to), rating = num(f.rating), pop = num(f.pop);
    let shown = 0;
    for (const row of rows) {
      const d = row.dataset;
      const rowTags = d.tags ? d.tags.split(",") : [];
      const year = num(d.year);
      const ok = words.every(w => d.name.includes(w))
        && tags.every(t => rowTags.includes(t))
        && (!yFrom || (year && year >= yFrom))
        && (!yTo || (year && year <= yTo))
        && (!rating || (num(d.rating) || 0) >= rating)
        && (!pop || (num(d.reviews) || 0) >= pop);
      row.hidden = !ok;
      shown += ok;
    }
    rows.slice().sort(sorters[f.sort] || sorters.playtime).forEach(r => list.append(r));
    const filtering = words.length || tags.length || yFrom || yTo || rating || pop;
    count.textContent = filtering ? `найдено: ${shown} из ${rows.length}` : "";
    empty.hidden = shown > 0;
  }

  importForm.addEventListener("input", () => { touched = true; apply(); });
  importForm.addEventListener("change", () => { touched = true; apply(); });
  importForm.addEventListener("reset", () => setTimeout(apply));

  // пока в фоне догружаются жанры и годы — показываем ход; когда готово, обновляем страницу,
  // если фильтрами ещё не пользовались (иначе предлагаем обновить, чтобы не сбросить выбор)
  const status = document.getElementById("meta-status");
  if (status) {
    const text = status.querySelector("[data-meta-text]");
    const poll = setInterval(async () => {
      let s;
      try { s = await (await fetch("/api/library-meta")).json(); } catch { return; }
      if (s.state === "running") {
        text.textContent = `Загружаю жанры и годы выхода: ${s.done} из ${s.total}`;
        return;
      }
      clearInterval(poll);
      if (s.state === "done" && !touched) { location.reload(); return; }
      status.classList.remove("running");
      status.querySelector(".spinner")?.remove();
      text.innerHTML = s.state === "done"
        ? 'Жанры и годы загружены. <a href="">Обновить страницу</a>, чтобы фильтры работали для всех игр.'
        : "Не удалось загрузить жанры для части игр — фильтры работают по тем, что есть.";
    }, 3000);
  }
}
