// The phone layout (SEM-136), for what indah 0.11 cannot do from Python: a stylesheet for narrow
// screens, tables drawn as cards, and the diff in its unified layout. Replace this with indah's
// own support when it has it (see docs/INDAH-REQUESTS-PROMPT.md). 560 px is indah's breakpoint.
const NARROW = "(max-width: 560px)";
const CSS = `@media ${NARROW} {
  button, select, input[type="text"], input[type="search"], input[type="number"], textarea,
  a.navitem, .choice-opt { min-height: 44px; }
  button, .choice-opt { min-width: 44px; }
  button { white-space: nowrap; }
  .row.fit > .field, .row.fit > button { flex: 1 1 100%; min-width: 0; }
  .choice.segmented { width: 100%; }
  .choice.segmented .choice-opt { flex: 1; }
  code { overflow-wrap: anywhere; white-space: normal; }
  table.diff td { word-break: normal !important; overflow-wrap: anywhere !important; }
  .table.rich thead { display: none; }
  .table.rich, .table.rich tbody, .table.rich tr, .table.rich td { display: block; width: auto; }
  .table.rich tr { margin: 0 0 .5rem; padding: .5rem .75rem; border: 1px solid var(--outline-soft);
    border-radius: 12px; background: var(--surface); }
  .table.rich tr.selected { background: var(--primary-container); border-color: var(--primary); }
  .table.rich[role=grid]:focus-visible tr.kb-active { outline: 2px solid var(--primary); }
  .table.rich[role=grid]:focus-visible tr.kb-active > td { box-shadow: none !important; background: none; }
  .table.rich td { display: flex; gap: .75rem; justify-content: space-between; align-items: baseline;
    max-width: none !important; white-space: normal; overflow-wrap: anywhere; border: 0;
    padding: .2rem 0; text-align: right; }
  .table.rich td:empty { display: none; }
  .table.rich td::before { content: attr(data-label); flex: 0 0 6.5rem; color: var(--muted);
    font-size: .75rem; text-transform: uppercase; letter-spacing: .04em; text-align: left; }
  .table.rich td.actions-col { flex-wrap: wrap; justify-content: flex-start; gap: .5rem; padding-top: .5rem; }
  .table.rich td.actions-col::before { display: none; }
  .table.rich td.actions-col > button { flex: 1 1 auto; }
  .table.rich td:first-child { font-weight: 600; text-align: left; }
  .table.rich td:first-child::before { display: none; }
}`;

export default class PhoneLayout extends HTMLElement {
  connectedCallback() {
    this.style.display = "none";
    this.sheet = new CSSStyleSheet();
    this.sheet.replaceSync(CSS);
    document.adoptedStyleSheets = [...document.adoptedStyleSheets, this.sheet];
    this.narrow = matchMedia(NARROW);
    this.pass = () => {
      this.queued = false;
      if (this.narrow.matches) this.adapt();
    };
    this.queue = () => {
      if (!this.queued) {
        this.queued = true;
        requestAnimationFrame(this.pass);
      }
    };
    this.watch = new MutationObserver(this.queue);
    this.watch.observe(document.body, { childList: true, subtree: true });
    this.narrow.addEventListener("change", this.queue);
    this.queue();
  }

  adapt() {
    // A card row names each value with its column's title.
    for (const table of document.querySelectorAll("table.rich")) {
      const titles = [...table.querySelectorAll("thead th")].map((th) => th.innerText.trim());
      for (const cell of table.querySelectorAll("tbody td:not([data-label])")) {
        cell.dataset.label = titles[cell.cellIndex] ?? "";
      }
    }
    // A diff opens unified on a phone; the person can still choose split.
    for (const diff of document.querySelectorAll(".diffview:not([data-auto])")) {
      const unified = [...diff.querySelectorAll(".diff-mode button")].find(
        (b) => b.textContent.trim() === "Unified",
      );
      if (!unified) continue;
      diff.dataset.auto = "1";
      unified.click();
    }
  }

  disconnectedCallback() {
    this.watch?.disconnect();
    this.narrow?.removeEventListener("change", this.queue);
    document.adoptedStyleSheets = document.adoptedStyleSheets.filter((s) => s !== this.sheet);
  }
}
