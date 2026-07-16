import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { App } from "./App";

describe("App", () => {
  it("renders the development-readiness baseline", () => {
    expect(renderToStaticMarkup(<App />)).toContain("Development Readiness");
  });
});
