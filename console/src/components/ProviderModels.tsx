import {
  Alert,
  Badge,
  Button,
  Checkbox,
  Group,
  Loader,
  Modal,
  MultiSelect,
  NumberInput,
  ScrollArea,
  Stack,
  Table,
  Text,
  TextInput,
} from "@mantine/core";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, type RegisterEntry } from "../api";
import { notifyError, notifyOk } from "./common";
import { SetupSteps } from "./SetupSteps";

interface LiveModel {
  id: string;
  added_as: string | null;
  suggested_price: {
    input_usd_per_m: number;
    output_usd_per_m: number;
    source: string;
  } | null;
}

interface Pick {
  on: boolean;
  input: number | string;
  output: number | string;
}

/** Right after an AI key is saved (or from its row): the provider's chat models, fetched
 * live with that key. Tick the ones people may use; the price is pre-filled from the
 * provider's list price where Captain knows it (check it), and the models are allowed for
 * the chosen AI uses. Then the remaining setup steps (budget...) show their state. */
export function ProviderModelsModal({
  providerId,
  onClose,
}: {
  providerId: string;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const live = useQuery({
    queryKey: ["provider-models", providerId],
    queryFn: () =>
      api.get<{ models: LiveModel[] }>(
        `/ai-keys/${encodeURIComponent(providerId)}/models`,
      ),
    retry: false,
  });
  const reg = useQuery({
    queryKey: ["register"],
    queryFn: () => api.get<RegisterEntry[]>("/register"),
  });
  const uses = (reg.data ?? []).filter((r) => r.active);
  const [picks, setPicks] = useState<Record<string, Pick>>({});
  const [forUses, setForUses] = useState<string[] | null>(null);
  const [filter, setFilter] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const chosenUses = forUses ?? uses.map((u) => u.id);

  const pick = (m: LiveModel): Pick =>
    picks[m.id] ?? {
      on: false,
      input: m.suggested_price?.input_usd_per_m ?? "",
      output: m.suggested_price?.output_usd_per_m ?? "",
    };
  const set = (m: LiveModel, p: Partial<Pick>) =>
    setPicks((x) => ({ ...x, [m.id]: { ...pick(m), ...p } }));
  const models = (live.data?.models ?? []).filter((m) =>
    m.id.toLowerCase().includes(filter.trim().toLowerCase()),
  );
  const chosen = (live.data?.models ?? []).filter((m) => pick(m).on);
  const ready =
    chosen.length > 0 &&
    chosen.every((m) => pick(m).input !== "" && pick(m).output !== "");

  return (
    <Modal
      opened
      onClose={onClose}
      size="xl"
      centered
      title={`Models from ${providerId}`}
    >
      {done ? (
        <Stack>
          <Alert color="teal">
            {chosen.length} model{chosen.length === 1 ? "" : "s"} added with
            prices. What is left before people can use AI:
          </Alert>
          <SetupSteps upTo="budget" />
          <Group justify="flex-end">
            <Button onClick={onClose}>Done</Button>
          </Group>
        </Stack>
      ) : live.isLoading ? (
        <Group>
          <Loader size="sm" />
          <Text size="sm">Asking {providerId} which models this key can use…</Text>
        </Group>
      ) : live.error ? (
        <Alert color="red" title="The provider could not list its models">
          {live.error instanceof Error ? live.error.message : String(live.error)}
        </Alert>
      ) : (
        <Stack>
          <Text size="sm" c="dimmed">
            Fetched live from the provider with this key. Tick the models people
            may use. Prices (US$ per 1 million tokens) load automatically from a
            daily-updated public price list; enter one only where none is known.
            A different contract price can be set later on Models &amp; tools.
          </Text>
          <TextInput
            placeholder="Filter, e.g. mini"
            value={filter}
            onChange={(e) => setFilter(e.currentTarget.value)}
          />
          <ScrollArea h={320}>
            <Table striped>
              <Table.Thead>
                <Table.Tr>
                  <Table.Th />
                  <Table.Th>Model</Table.Th>
                  <Table.Th w={130}>Input price</Table.Th>
                  <Table.Th w={130}>Output price</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {models.map((m) => {
                  const p = pick(m);
                  return (
                    <Table.Tr key={m.id}>
                      <Table.Td>
                        {m.added_as ? (
                          <Badge variant="light" color="teal">
                            Added
                          </Badge>
                        ) : (
                          <Checkbox
                            aria-label={`Add ${m.id}`}
                            checked={p.on}
                            onChange={(e) =>
                              set(m, { on: e.currentTarget.checked })
                            }
                          />
                        )}
                      </Table.Td>
                      <Table.Td>
                        <Text size="sm">{m.id}</Text>
                        {!m.suggested_price && !m.added_as && (
                          <Text size="xs" c="dimmed">
                            No list price known: enter it
                          </Text>
                        )}
                      </Table.Td>
                      <Table.Td>
                        <NumberInput
                          size="xs"
                          min={0}
                          decimalScale={4}
                          prefix="$"
                          disabled={!!m.added_as}
                          readOnly={!!m.suggested_price}
                          variant={m.suggested_price ? "filled" : "default"}
                          value={p.input}
                          onChange={(v) => set(m, { input: v })}
                        />
                      </Table.Td>
                      <Table.Td>
                        <NumberInput
                          size="xs"
                          min={0}
                          decimalScale={4}
                          prefix="$"
                          disabled={!!m.added_as}
                          readOnly={!!m.suggested_price}
                          variant={m.suggested_price ? "filled" : "default"}
                          value={p.output}
                          onChange={(v) => set(m, { output: v })}
                        />
                      </Table.Td>
                    </Table.Tr>
                  );
                })}
              </Table.Tbody>
            </Table>
            {models.length === 0 && (
              <Text c="dimmed" size="sm" p="sm">
                No chat models found.
              </Text>
            )}
          </ScrollArea>
          {uses.length > 0 && (
            <MultiSelect
              label="Allow them for these AI uses"
              description="Keys of these AI uses may then use the models"
              data={uses.map((u) => ({ value: u.id, label: u.name }))}
              value={chosenUses}
              onChange={setForUses}
            />
          )}
          <Group justify="flex-end">
            <Button variant="default" onClick={onClose}>
              Skip for now
            </Button>
            <Button
              loading={busy}
              disabled={!ready}
              onClick={async () => {
                setBusy(true);
                try {
                  const names: string[] = [];
                  for (const m of chosen) {
                    const p = pick(m);
                    await api.put("/registry/model", {
                      alias: m.id,
                      provider_id: providerId,
                      provider_model: m.id,
                      region: "external",
                      capabilities: ["chat", "streaming", "tools"],
                    });
                    await api.put("/registry/route", {
                      alias: m.id,
                      targets: [m.id],
                    });
                    await api.put(`/prices/${encodeURIComponent(m.id)}`, {
                      input_usd_per_m: Number(p.input),
                      output_usd_per_m: Number(p.output),
                    });
                    names.push(m.id);
                  }
                  for (const r of uses.filter((u) => chosenUses.includes(u.id))) {
                    const add = names.filter((n) => !r.allowed_models.includes(n));
                    if (add.length)
                      await api.put(`/register/${r.id}`, {
                        entry: { ...r, allowed_models: [...r.allowed_models, ...add] },
                        approval_ref: null,
                      });
                  }
                  notifyOk(`${names.length} model(s) added`);
                  await Promise.all(
                    ["registry", "register", "prices", "readiness", "provider-models"].map(
                      (k) => qc.invalidateQueries({ queryKey: [k] }),
                    ),
                  );
                  setDone(true);
                } catch (e) {
                  notifyError(e);
                } finally {
                  setBusy(false);
                }
              }}
            >
              Add {chosen.length || ""} model{chosen.length === 1 ? "" : "s"}
            </Button>
          </Group>
        </Stack>
      )}
    </Modal>
  );
}
