import React from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { InformationControl } from "./InformationControl";

describe("C283-4 shared information control", () => {
  it("starts closed, opens from the keyboard, and restores trigger focus on Escape", async () => {
    const user = userEvent.setup();
    const view = render(
      <>
        <button type="button">Background action</button>
        <InformationControl label="Test panel">
          <p>Detailed explanation</p>
          <button type="button">Dialog action</button>
        </InformationControl>
      </>,
    );

    const trigger = screen.getByRole("button", { name: "Information about Test panel" });
    const background = screen.getByRole("button", { name: "Background action" });
    expect(trigger).toHaveTextContent(/^i$/);
    expect(trigger).toHaveAttribute("title", "Information about Test panel");
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("Detailed explanation")).not.toBeInTheDocument();

    trigger.focus();
    await user.keyboard("{Enter}");
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("dialog", { name: "Test panel" })).toHaveTextContent(
      "Detailed explanation",
    );
    const close = screen.getByRole("button", { name: "Close" });
    const dialogAction = screen.getByRole("button", { name: "Dialog action" });
    expect(close).toHaveFocus();
    expect(view.container).toHaveAttribute("inert");

    await user.tab();
    expect(dialogAction).toHaveFocus();
    await user.tab();
    expect(close).toHaveFocus();
    await user.tab({ shift: true });
    expect(dialogAction).toHaveFocus();
    expect(background).not.toHaveFocus();

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Test panel" })).not.toBeInTheDocument();
    await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(trigger).toHaveFocus();
    expect(view.container).not.toHaveAttribute("inert");
  });
});
