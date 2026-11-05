// "Skip to content" for the dashboard (SEM-137). indah's AppShell has no skip link and no
// landmark on its main region, so this element adds both: a link that is the first stop of
// the page, hidden until it has focus, which moves focus into the main region.
const HIDDEN =
  "position:absolute;left:-9999px;top:0;z-index:10;padding:.5rem 1rem;border-radius:999px;" +
  "background:var(--primary);color:var(--on-primary);font:inherit;font-weight:600;" +
  "text-decoration:none;outline:2px solid var(--on-primary);outline-offset:-4px";
const SHOWN = HIDDEN.replace("left:-9999px", "left:.75rem;top:.75rem");

export default class SkipLink extends HTMLElement {
  connectedCallback() {
    this.style.display = "none";
    const main = document.querySelector(".appshell .main");
    const shell = document.querySelector(".shell");
    if (!main || !shell || shell.querySelector(".skip-link")) return;
    main.setAttribute("role", "main");
    main.id = "main";
    main.tabIndex = -1;
    const link = document.createElement("a");
    link.className = "skip-link";
    link.href = "#main";
    link.textContent = "Skip to content";
    link.style.cssText = HIDDEN;
    link.addEventListener("focus", () => (link.style.cssText = SHOWN));
    link.addEventListener("blur", () => (link.style.cssText = HIDDEN));
    link.addEventListener("click", (event) => {
      event.preventDefault(); // a hash would be read as an in-app path
      main.focus();
    });
    shell.prepend(link);
    this.link = link;
  }

  disconnectedCallback() {
    this.link?.remove();
  }
}
