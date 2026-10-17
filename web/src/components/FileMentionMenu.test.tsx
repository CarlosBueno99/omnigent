import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { FileMentionMenu } from "./FileMentionMenu";

afterEach(cleanup);

describe("menu surface", () => {
  it("carries the dark glass blur so the translucent dark popover does not ghost the transcript", () => {
    // Built-in dark palettes give --popover an alpha; the blur keeps the
    // transcript behind the floating menu from reading through it.
    render(
      <FileMentionMenu
        currentDir=""
        activeIndex={0}
        entries={[
          { path: "README.md", name: "README.md", type: "file", bytes: 12, modified_at: null },
        ]}
        onOpenDir={() => undefined}
        onAttach={() => undefined}
      />,
    );
    expect(screen.getByTestId("file-mention-menu")).toHaveClass(
      "dark:backdrop-blur-xl",
      "dark:backdrop-saturate-150",
    );
  });
});
