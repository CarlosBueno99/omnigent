import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { showToast } from "@/components/ui/toast";
import { getProjectOrder, saveProjectOrder, type ProjectOrder } from "@/lib/projectsApi";
import { resolveOrCreateProjectId, type ProjectSummary } from "./useConversations";

export function useProjectOrder() {
  return useQuery({ queryKey: ["project-order"], queryFn: getProjectOrder });
}

function orderProjects(projects: ProjectSummary[], names: string[] | null) {
  const ranks = new Map(names?.map((name, index) => [name, index]));
  return [...projects].sort((left, right) =>
    names === null
      ? left.name < right.name
        ? -1
        : left.name > right.name
          ? 1
          : 0
      : (ranks.get(left.name) ?? Infinity) - (ranks.get(right.name) ?? Infinity),
  );
}

const confirmedOrders = new WeakMap<
  QueryClient,
  { names: string[] | null; preference: ProjectOrder | undefined }
>();

export function useSaveProjectOrder() {
  const client = useQueryClient();
  return useMutation({
    mutationKey: ["project-order"],
    scope: { id: "project-order" },
    mutationFn: async (projects: ProjectSummary[] | null) => {
      if (projects === null) return saveProjectOrder(null);
      const ids = await Promise.all(
        projects.map((project) => project.id ?? resolveOrCreateProjectId(project.name)),
      );
      return saveProjectOrder(ids);
    },
    onMutate: async (projects) => {
      await Promise.all([
        client.cancelQueries({ queryKey: ["projects"] }),
        client.cancelQueries({ queryKey: ["project-order"] }),
      ]);
      const current = client.getQueryData<ProjectSummary[]>(["projects"]) ?? [];
      if (!confirmedOrders.has(client))
        confirmedOrders.set(client, {
          names: current.map((project) => project.name),
          preference: client.getQueryData<ProjectOrder>(["project-order"]),
        });
      client.setQueryData(
        ["projects"],
        orderProjects(current, projects?.map((project) => project.name) ?? null),
      );
      client.setQueryData<ProjectOrder>(["project-order"], (previous) => ({
        sort_mode: projects ? "manual" : "alphabetical",
        ordered_project_ids: projects
          ? projects.flatMap((project) => (project.id ? [project.id] : []))
          : (previous?.ordered_project_ids ?? null),
      }));
    },
    onSuccess: (preference, projects) => {
      confirmedOrders.set(client, {
        names: projects?.map((project) => project.name) ?? null,
        preference,
      });
      if (client.isMutating({ mutationKey: ["project-order"] }) === 1)
        client.setQueryData(["project-order"], preference);
    },
    onError: () => {
      const confirmed = confirmedOrders.get(client);
      if (confirmed && client.isMutating({ mutationKey: ["project-order"] }) === 1) {
        client.setQueryData<ProjectSummary[]>(["projects"], (current) =>
          current ? orderProjects(current, confirmed.names) : current,
        );
        if (confirmed.preference) client.setQueryData(["project-order"], confirmed.preference);
      }
      showToast("Couldn't save project order", { duration: 0 });
    },
    onSettled: () => {
      if (client.isMutating({ mutationKey: ["project-order"] }) > 1) return;
      confirmedOrders.delete(client);
      void client.invalidateQueries({ queryKey: ["projects"] });
      void client.invalidateQueries({ queryKey: ["project-order"] });
    },
  });
}
