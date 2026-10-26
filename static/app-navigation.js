const appShell = document.querySelector("[data-app-shell]");

if (appShell) {
  const appMain = document.querySelector("[data-app-main]");
  let navigationController;

  function updateActiveNavigation(url) {
    const target = new URL(url, window.location.href);
    const category = target.searchParams.get("category") || "";
    const query = target.searchParams.get("q") || "";
    const listingType = target.searchParams.get("type") || "";

    appShell.querySelectorAll("[data-app-nav]").forEach((link) => {
      const key = link.dataset.navKey;
      let selected = false;

      if (key === "browse") {
        selected = target.pathname === "/" && !category;
      } else if (key === "category") {
        selected = target.pathname === "/" && link.dataset.navCategory === category;
      } else if (key === "buying") {
        selected =
          target.pathname === "/transactions" &&
          target.searchParams.get("view") === "buyer";
      } else if (key === "selling") {
        selected =
          target.pathname === "/my-listings" ||
          (target.pathname === "/transactions" &&
            target.searchParams.get("view") === "seller");
      } else {
        const destinations = {
          notifications: "/notifications",
          inbox: "/messages",
          "create-listing": "/listings/new",
        };
        selected = target.pathname === destinations[key];
      }

      link.classList.toggle("selected", selected);
      if (selected) {
        link.setAttribute("aria-current", "page");
      } else {
        link.removeAttribute("aria-current");
      }

      if (key === "category" && query) {
        const linkUrl = new URL(link.href);
        linkUrl.searchParams.set("q", query);
        if (listingType) {
          linkUrl.searchParams.set("type", listingType);
        }
        link.href = linkUrl.href;
      } else if (key === "category") {
        const linkUrl = new URL(link.href);
        linkUrl.searchParams.delete("q");
        if (listingType) {
          linkUrl.searchParams.set("type", listingType);
        } else {
          linkUrl.searchParams.delete("type");
        }
        link.href = linkUrl.href;
      }
    });

    const marketplaceLink = document.querySelector(".marketplace-primary-link");
    if (marketplaceLink) {
      const selected = target.pathname === "/";
      marketplaceLink.classList.toggle("active", selected);
      if (selected) {
        marketplaceLink.setAttribute("aria-current", "page");
      } else {
        marketplaceLink.removeAttribute("aria-current");
      }
    }

    const searchForm = document.querySelector(".marketplace-top-search");
    const searchInput = searchForm?.querySelector("input[name='q']");
    if (searchInput) {
      searchInput.value = query;
    }
    if (searchForm) {
      let categoryInput = searchForm.querySelector("input[name='category']");
      if (category) {
        if (!categoryInput) {
          categoryInput = document.createElement("input");
          categoryInput.type = "hidden";
          categoryInput.name = "category";
          searchForm.append(categoryInput);
        }
        categoryInput.value = category;
      } else {
        categoryInput?.remove();
      }
      let typeInput = searchForm.querySelector("input[name='type']");
      if (listingType) {
        if (!typeInput) {
          typeInput = document.createElement("input");
          typeInput.type = "hidden";
          typeInput.name = "type";
          searchForm.append(typeInput);
        }
        typeInput.value = listingType;
      } else {
        typeInput?.remove();
      }
    }
  }

  async function loadPage(url, { addHistory = true } = {}) {
    navigationController?.abort();
    const controller = new AbortController();
    navigationController = controller;
    appMain.setAttribute("aria-busy", "true");

    try {
      const response = await fetch(url, {
        headers: { Accept: "text/html", "X-Requested-With": "XMLHttpRequest" },
        signal: controller.signal,
      });
      if (!response.ok) {
        window.location.assign(url);
        return;
      }

      const html = await response.text();
      const documentFragment = new DOMParser().parseFromString(html, "text/html");
      const nextContent = documentFragment.querySelector("#page-content");
      if (!nextContent || !documentFragment.querySelector("[data-app-shell]")) {
        window.location.assign(response.url);
        return;
      }

      document.querySelector("#page-content").replaceChildren(
        ...Array.from(nextContent.childNodes, (node) => node.cloneNode(true)),
      );
      document.title = documentFragment.title;
      if (addHistory) {
        window.history.pushState({}, "", response.url);
      }
      updateActiveNavigation(response.url);
      appMain.scrollTop = 0;
      appMain.focus({ preventScroll: true });
    } catch (error) {
      if (error.name !== "AbortError") {
        window.location.assign(url);
      }
    } finally {
      if (!controller.signal.aborted) {
        appMain.removeAttribute("aria-busy");
      }
    }
  }

  document.addEventListener("click", (event) => {
    const link = event.target.closest("a[data-app-nav]");
    if (
      !link ||
      event.defaultPrevented ||
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey ||
      link.target ||
      link.hasAttribute("download")
    ) {
      return;
    }

    const url = new URL(link.href, window.location.href);
    if (url.origin !== window.location.origin) {
      return;
    }
    event.preventDefault();
    loadPage(url.href);
  });

  document.addEventListener("submit", (event) => {
    const form = event.target.closest("form[data-app-search]");
    if (!form || form.method.toLowerCase() !== "get") {
      return;
    }

    const url = new URL(form.action, window.location.href);
    new FormData(form).forEach((value, key) => {
      if (typeof value === "string" && value) {
        url.searchParams.set(key, value);
      } else {
        url.searchParams.delete(key);
      }
    });
    event.preventDefault();
    loadPage(url.href);
  });

  window.addEventListener("popstate", () => {
    loadPage(window.location.href, { addHistory: false });
  });

  updateActiveNavigation(window.location.href);
}
