import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(() => cleanup());

class TestResizeObserver implements ResizeObserver {
  readonly observed: Element[] = [];

  constructor(private readonly callback: ResizeObserverCallback) {
    void this.callback;
  }

  disconnect(): void {
    this.observed.length = 0;
  }

  observe(target: Element): void {
    this.observed.push(target);
  }

  unobserve(target: Element): void {
    const index = this.observed.indexOf(target);
    if (index >= 0) this.observed.splice(index, 1);
  }
}

globalThis.ResizeObserver = TestResizeObserver;
