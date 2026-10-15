import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";

// jsdom has no matchMedia; the theme hook asks it for the system setting.
Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: (query: string) => ({
    matches: false,
    media: query,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
  }),
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  localStorage.clear();
  window.location.hash = "";
  delete document.documentElement.dataset.theme;
});
