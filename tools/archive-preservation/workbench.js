"use strict";

function bindKurtMasterChoices(root = document) {
  for (const input of root.querySelectorAll("input[data-master-choice]")) {
    if (input.dataset.masterBound === "yes") continue;
    input.dataset.masterBound = "yes";
    input.addEventListener("change", () => {
      if (!input.checked) return;
      for (const other of document.querySelectorAll("input[data-master-choice]")) {
        if (other !== input) other.checked = false;
      }
    });
  }
}

function bindAsyncReconciliationForms(root = document) {
  for (const form of root.querySelectorAll("form[data-async-save]")) {
    if (form.dataset.asyncBound === "yes") continue;
    form.dataset.asyncBound = "yes";
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = form.querySelector('button[type="submit"]') || (
        form.id
          ? document.querySelector(`button[type="submit"][form="${form.id}"]`)
          : null
      );
      const originalLabel = button ? button.textContent : "";
      const scrollPosition = window.scrollY;
      if (button) {
        button.disabled = true;
        button.textContent = "Saving…";
      }
      form.setAttribute("aria-busy", "true");
      try {
        const action = form.getAttribute("action");
        if (!action) throw new Error("Workbench form action is missing");
        const removeSelector = form.dataset.removeOnSave;
        const removablePanel = removeSelector ? form.closest(removeSelector) : null;
        const formData = new FormData(form);
        const selectedDisposition = formData.get("disposition");
        const response = await fetch(action, {
          method: "POST",
          body: new URLSearchParams(formData),
          credentials: "same-origin",
          headers: { "X-Workbench-Async": "1" },
        });
        if (!response.ok) throw new Error(`Workbench returned HTTP ${response.status}`);
        if (response.status === 204 && removablePanel) {
          if (selectedDisposition === "selected_master") {
            const mediaGrid = removablePanel.closest(".kurt-media-grid");
            if (mediaGrid) {
              mediaGrid.innerHTML = '<div class="notice notice--safe">Master recorded. All other candidates were marked redundant; explicit Wrong decisions remain Wrong.</div>';
              return;
            }
          }
          const nextPanel = removablePanel.nextElementSibling;
          removablePanel.remove();
          if (nextPanel instanceof HTMLElement) {
            nextPanel.setAttribute("tabindex", "-1");
            nextPanel.focus({ preventScroll: true });
          }
          return;
        }
        const documentText = await response.text();
        const parsed = new DOMParser().parseFromString(documentText, "text/html");
        const replacement = parsed.querySelector("main.main");
        const current = document.querySelector("main.main");
        if (!replacement || !current) throw new Error("Workbench response is incomplete");
        current.replaceWith(replacement);
        document.title = parsed.title || document.title;
        history.replaceState({}, "", response.url);
        bindAsyncReconciliationForms(replacement);
        window.scrollTo({
          top: form.dataset.mediaBatchSave === "yes" ? 0 : scrollPosition,
          behavior: "auto",
        });
      } catch (error) {
        form.removeAttribute("aria-busy");
        if (button) {
          button.disabled = false;
          button.textContent = originalLabel;
        }
        window.alert(`Save failed: ${error.message}`);
      }
    });
  }
  bindKurtMasterChoices(root);
}

document.addEventListener("DOMContentLoaded", () => {
  bindAsyncReconciliationForms();
});
