import { afterEach, expect, it } from "vitest";
import { openSameOriginSessionLink } from "./sessionNavigation";

const ORIGIN = window.location.origin;

afterEach(() => {
  delete window.__OMNIGENT_BASE_PATH__;
  window.history.replaceState(null, "", "/");
});

it.each([
  ["", "/c/conv-2", "/c/conv-2"],
  ["", "/canvas/c/conv-2", "/canvas/c/conv-2"],
  ["/proxy/6767", "/proxy/6767/canvas/c/conv-2", "/proxy/6767/canvas/c/conv-2"],
])("opens a session route in place (base %j, link %s)", (base, path, expected) => {
  window.__OMNIGENT_BASE_PATH__ = base;
  expect(openSameOriginSessionLink(`${ORIGIN}${path}`)).toBe(true);
  expect(window.location.pathname).toBe(expected);
});

it.each([
  ["", "/settings/c/session"],
  ["", "/v1/sessions/conv-1/c/x"],
  ["", "/c/conv-2/extra"],
  ["/proxy/6767", "/proxy/6767/settings/c/session"],
])("leaves a non-session path alone (base %j, link %s)", (base, path) => {
  window.__OMNIGENT_BASE_PATH__ = base;
  expect(openSameOriginSessionLink(`${ORIGIN}${path}`)).toBe(false);
  expect(window.location.pathname).toBe("/");
});
