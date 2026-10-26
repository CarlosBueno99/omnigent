import { beforeEach, expect, it, vi } from "vitest";
import { authenticatedFetch } from "./identity";
import { fetchSandboxProfiles } from "./sandboxProfilesApi";

vi.mock("./identity", () => ({ authenticatedFetch: vi.fn() }));

beforeEach(() => vi.mocked(authenticatedFetch).mockReset());

it("keeps discovery scoped to the selected provider across pages", async () => {
  const page = (id: string, next_cursor: string | null) =>
    new Response(
      JSON.stringify({
        profiles: [{ id, provider: "company" }],
        managed_providers: ["company"],
        next_cursor,
      }),
    );
  vi.mocked(authenticatedFetch)
    .mockResolvedValueOnce(page("small", "first"))
    .mockResolvedValueOnce(page("large", null));
  const result = await fetchSandboxProfiles("company");
  expect(result.profiles.map((p) => p.id)).toEqual(["small", "large"]);
  expect(result.managed_providers).toEqual(["company"]);
  expect(result.next_cursor).toBeNull();
  expect(vi.mocked(authenticatedFetch).mock.calls).toEqual([
    ["/v1/sandbox-profiles?limit=100&provider=company", undefined],
    ["/v1/sandbox-profiles?limit=100&provider=company&cursor=first", undefined],
  ]);
});

it.each([
  [{ error: { message: "Provider unavailable" } }, "Provider unavailable"],
  [{ detail: "Choose another profile" }, "Choose another profile"],
  [{ detail: [{ message: "not a public message" }] }, "Sandbox profile request failed (503)."],
  ["invalid JSON", "Sandbox profile request failed (503)."],
])("extracts a useful error from %j", async (body, message) => {
  vi.mocked(authenticatedFetch).mockResolvedValueOnce(
    new Response(typeof body === "string" ? body : JSON.stringify(body), { status: 503 }),
  );
  await expect(fetchSandboxProfiles("company")).rejects.toThrow(message);
});

it("stops when discovery repeats a cursor", async () => {
  vi.mocked(authenticatedFetch).mockImplementation(
    async () =>
      new Response(
        JSON.stringify({
          profiles: [],
          managed_providers: ["company"],
          next_cursor: "same-page",
        }),
      ),
  );
  await expect(fetchSandboxProfiles("company")).rejects.toThrow("repeated cursor");
  expect(authenticatedFetch).toHaveBeenCalledTimes(2);
});
