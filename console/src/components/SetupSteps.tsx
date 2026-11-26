import {
  Badge,
  Button,
  Group,
  Paper,
  Stack,
  Text,
  ThemeIcon,
} from "@mantine/core";
import { IconCheck } from "@tabler/icons-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, type RegisterEntry } from "../api";
import { KeyModal } from "../pages/AiKeys";
import { BudgetModal } from "../pages/Budgets";
import { EntryModal } from "../pages/Register";
import { AddModel, SetPrice } from "../pages/Registry";

export interface Step {
  key: string;
  label: string;
  done: boolean;
  detail: string;
  missing?: string[];
}
export interface Readiness {
  steps: Step[];
  ready_for_keys: boolean;
}

export function useReadiness() {
  return useQuery({
    queryKey: ["readiness"],
    queryFn: () => api.get<Readiness>("/readiness"),
  });
}

/** The steps a company needs before its first AI call. Each one is checked live and its
 * form opens right here, so the org admin never has to hunt through the menu. */
export function SetupSteps({
  upTo,
  onKeyStep,
}: {
  upTo?: string;
  onKeyStep?: () => void;
}) {
  const qc = useQueryClient();
  const q = useReadiness();
  const reg = useQuery({
    queryKey: ["register"],
    queryFn: () => api.get<RegisterEntry[]>("/register"),
  });
  const kinds = useQuery({
    queryKey: ["ai-keys"],
    queryFn: () => api.get<{ kinds: string[] }>("/ai-keys"),
  });
  const [open, setOpen] = useState<string | null>(null);
  const refresh = async () => {
    for (const k of [
      "readiness",
      "register",
      "registry",
      "prices",
      "budgets",
      "ai-keys",
      "keys",
    ])
      await qc.invalidateQueries({ queryKey: [k] });
  };
  const close = () => {
    setOpen(null);
    void refresh();
  };
  if (!q.data) return null;
  const all = q.data.steps;
  const steps = upTo
    ? all.slice(0, all.findIndex((s) => s.key === upTo) + 1 || all.length)
    : all;
  const next = steps.find((s) => !s.done)?.key;
  const price = all.find((s) => s.key === "price");
  return (
    <Stack gap={6}>
      {steps.map((s, i) => (
        <Paper
          key={s.key}
          withBorder
          p="xs"
          bg={s.key === next ? "var(--mantine-color-blue-light)" : undefined}
        >
          <Group justify="space-between" wrap="nowrap">
            <Group gap="sm" wrap="nowrap">
              <ThemeIcon
                size="md"
                radius="xl"
                color={s.done ? "green" : s.key === next ? "blue" : "gray"}
                variant={s.done ? "filled" : "light"}
              >
                {s.done ? (
                  <IconCheck size={14} />
                ) : (
                  <Text size="xs">{i + 1}</Text>
                )}
              </ThemeIcon>
              <div>
                <Text size="sm" fw={500}>
                  {s.label}
                </Text>
                <Text size="xs" c="dimmed">
                  {s.detail}
                </Text>
              </div>
            </Group>
            {s.done ? (
              <Badge color="green" variant="light">
                Done
              </Badge>
            ) : (
              <Button
                size="xs"
                variant={s.key === next ? "filled" : "default"}
                onClick={() =>
                  s.key === "captain_key" && onKeyStep
                    ? onKeyStep()
                    : setOpen(s.key)
                }
              >
                {s.key === next ? "Do it now" : "Open"}
              </Button>
            )}
          </Group>
        </Paper>
      ))}
      {open === "ai_key" && (
        <KeyModal
          initial={{ provider_id: "", kind: "openai", base_url: "" }}
          kinds={kinds.data?.kinds ?? ["openai"]}
          onClose={close}
          onSaved={async () => close()}
        />
      )}
      {open === "ai_use" && (
        <EntryModal
          entry={null}
          taken={(reg.data ?? []).map((r) => r.id)}
          onClose={close}
        />
      )}
      {open === "model" && <AddModel onClose={close} />}
      {open === "price" && price?.missing?.[0] && (
        <SetPrice alias={price.missing[0]} onClose={close} />
      )}
      {open === "price" && !price?.missing?.length && (
        <AddModel onClose={close} />
      )}
      {open === "budget" && (
        <BudgetModal opened onClose={close} onSaved={() => void refresh()} />
      )}
    </Stack>
  );
}
