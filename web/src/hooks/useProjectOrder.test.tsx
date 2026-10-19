import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { saveProjectOrder } from "@/lib/projectsApi";
import { resolveOrCreateProjectId } from "./useConversations";
import { showToast } from "@/components/ui/toast";
import { useSaveProjectOrder } from "./useProjectOrder";

vi.mock("@/lib/projectsApi", () => ({ saveProjectOrder: vi.fn(), getProjectOrder: vi.fn() }));
vi.mock("./useConversations", () => ({ resolveOrCreateProjectId: vi.fn() }));
vi.mock("@/components/ui/toast", () => ({ showToast: vi.fn() }));

const projects = [
  { id: "a", name: "A" },
  { id: "b", name: "B" },
];
function setup() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  client.setQueryData(["projects"], projects);
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return { client, ...renderHook(() => useSaveProjectOrder(), { wrapper }) };
}

beforeEach(() => vi.resetAllMocks());

describe("saving project order", () => {
  it("previews a move immediately and restores confirmed order after a failed save", async () => {
    let reject!: (reason: Error) => void;
    vi.mocked(saveProjectOrder).mockReturnValue(
      new Promise((_resolve, rejectPromise) => {
        reject = rejectPromise;
      }),
    );
    const { result, client } = setup();
    act(() => result.current.mutate([...projects].reverse()));
    await waitFor(() => expect(saveProjectOrder).toHaveBeenCalledWith(["b", "a"]));
    expect(client.getQueryData(["projects"])).toEqual([...projects].reverse());
    act(() => reject(new Error("offline")));
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(client.getQueryData(["projects"])).toEqual(projects);
    expect(showToast).toHaveBeenCalledWith("Couldn't save project order", { duration: 0 });
  });

  it.each([false, true])(
    "queues newer intent and rolls back to the last confirmed order (first fails: %s)",
    async (firstFails) => {
      const writes: {
        resolve: (value: Awaited<ReturnType<typeof saveProjectOrder>>) => void;
        reject: (error: Error) => void;
      }[] = [];
      vi.mocked(saveProjectOrder).mockImplementation(
        () =>
          new Promise((resolve, reject) => {
            writes.push({ resolve, reject });
          }),
      );
      const { result, client } = setup();
      client.setQueryData(["project-order"], {
        sort_mode: "alphabetical",
        ordered_project_ids: null,
      });
      act(() => result.current.mutate([...projects].reverse()));
      await waitFor(() => expect(writes).toHaveLength(1));
      act(() => result.current.mutate(null));
      await waitFor(() => expect(client.getQueryData(["projects"])).toEqual(projects));
      expect(writes).toHaveLength(1);
      client.setQueryData(
        ["projects"],
        [{ ...projects[0], icon: "🔥" }, projects[1], { id: "c", name: "C" }],
      );
      act(() =>
        firstFails
          ? writes[0].reject(new Error("offline"))
          : writes[0].resolve({ sort_mode: "manual", ordered_project_ids: ["b", "a"] }),
      );
      await waitFor(() => expect(writes).toHaveLength(2));
      expect(client.getQueryData(["projects"])).toEqual([
        { ...projects[0], icon: "🔥" },
        projects[1],
        { id: "c", name: "C" },
      ]);
      act(() => writes[1].reject(new Error("offline")));
      await waitFor(() => expect(result.current.isError).toBe(true));
      expect(client.getQueryData(["projects"])).toEqual(
        firstFails
          ? [{ ...projects[0], icon: "🔥" }, projects[1], { id: "c", name: "C" }]
          : [projects[1], { ...projects[0], icon: "🔥" }, { id: "c", name: "C" }],
      );
      expect(client.getQueryData(["project-order"])).toEqual(
        firstFails
          ? { sort_mode: "alphabetical", ordered_project_ids: null }
          : { sort_mode: "manual", ordered_project_ids: ["b", "a"] },
      );
    },
  );

  it("promotes legacy folders while preserving the intended order", async () => {
    vi.mocked(resolveOrCreateProjectId).mockResolvedValue("legacy-id");
    vi.mocked(saveProjectOrder).mockResolvedValue({
      ordered_project_ids: ["legacy-id", "a"],
      sort_mode: "manual",
    });
    const { result } = setup();
    act(() => result.current.mutate([{ id: null, name: "Legacy" }, projects[0]]));
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(resolveOrCreateProjectId).toHaveBeenCalledWith("Legacy");
    expect(saveProjectOrder).toHaveBeenCalledWith(["legacy-id", "a"]);
  });

  it("resets without promoting folders and invalidates both queries", async () => {
    vi.mocked(saveProjectOrder).mockResolvedValue({
      ordered_project_ids: ["b", "a"],
      sort_mode: "alphabetical",
    });
    const { result, client } = setup();
    client.setQueryData(["project-order"], { ordered_project_ids: ["b", "a"] });
    act(() => result.current.mutate(null));
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(saveProjectOrder).toHaveBeenCalledWith(null);
    expect(resolveOrCreateProjectId).not.toHaveBeenCalled();
    expect(client.getQueryState(["projects"])?.isInvalidated).toBe(true);
    expect(client.getQueryState(["project-order"])?.isInvalidated).toBe(true);
  });
});
