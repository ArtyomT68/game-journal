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
