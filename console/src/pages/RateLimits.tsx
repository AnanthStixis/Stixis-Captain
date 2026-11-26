import { Button, Group, Modal, NumberInput, Paper, ScrollArea, Stack, Table, Text, TextInput, Title } from "@mantine/core";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, type RateLimit } from "../api";
import { can, useMe } from "../App";
import { QueryState, notifyError, notifyOk } from "../components/common";

/** Requests-per-minute limits. A per-tool limit uses the scope `tool:<name>` (everyone) or
 * `ptool:<who>:<name>` (one caller on one tool). Lifting a limit is recorded in the ledger. */
export function RateLimitsSection() {
  const me = useMe().data;
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["rate-limits"], queryFn: () => api.get<RateLimit[]>("/rate-limits") });
  const [open, setOpen] = useState(false);
  const refresh = () => qc.invalidateQueries({ queryKey: ["rate-limits"] });
  const writable = can(me, "budget:write");

  return (
    <Paper withBorder p="md" radius="md" mt="lg">
      <Group justify="space-between" mb="xs">
        <div>
          <Title order={5}>Rate limits</Title>
          <Text size="xs" c="dimmed">
            Requests and tokens per minute, on any budget scope (a person, key, application, project, model) or a tool. Use <code>tool:server.tool</code> to limit a tool for everyone, <code>ptool:person:server.tool</code> for one caller, or any budget scope.
          </Text>
        </div>
        {writable && <Button onClick={() => setOpen(true)}>Add rate limit</Button>}
      </Group>
      <QueryState isLoading={q.isLoading} error={q.error} empty={q.data?.length === 0} emptyText="No rate limits defined.">
        <ScrollArea>
          <Table miw={520}>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Scope</Table.Th>
                <Table.Th ta="right">Requests / minute</Table.Th>
                <Table.Th ta="right">Tokens / minute</Table.Th>
                <Table.Th />
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {q.data?.map((r) => (
                <Table.Tr key={r.scope}>
                  <Table.Td fw={600}>{r.scope}</Table.Td>
                  <Table.Td ta="right">{r.requests_per_minute ?? "–"}</Table.Td>
                  <Table.Td ta="right">{r.tokens_per_minute ?? "–"}</Table.Td>
                  <Table.Td ta="right">
                    {writable && (
                      <Button
                        size="xs"
                        variant="light"
                        color="red"
                        onClick={async () => {
                          if (!window.confirm(`Lift the rate limit on ${r.scope}? This is recorded in the evidence ledger.`)) return;
                          try {
                            await api.del(`/rate-limits?scope=${encodeURIComponent(r.scope)}`);
                            notifyOk("Rate limit lifted");
                            await refresh();
                          } catch (e) {
                            notifyError(e);
                          }
                        }}
                      >
                        Lift
                      </Button>
                    )}
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </ScrollArea>
      </QueryState>
      <RateLimitModal opened={open} onClose={() => setOpen(false)} onSaved={refresh} />
    </Paper>
  );
}

function RateLimitModal({ opened, onClose, onSaved }: { opened: boolean; onClose: () => void; onSaved: () => void }) {
  const [scope, setScope] = useState("tool:");
  const [rpm, setRpm] = useState<number | string>(30);
  const [tpm, setTpm] = useState<number | string>("");
  const [busy, setBusy] = useState(false);
  return (
    <Modal opened={opened} onClose={onClose} title="Add a rate limit" centered>
      <Stack>
        <TextInput label="Scope" description="tool:<server.tool> · ptool:<person>:<server.tool> · principal:<id> · app:<id> · model:<alias>" value={scope} onChange={(e) => setScope(e.currentTarget.value)} />
        <NumberInput label="Requests per minute" description="leave empty for no request limit" min={1} value={rpm} onChange={setRpm} />
        <NumberInput label="Tokens per minute" description="optional: input plus the most the call could produce; stops very large prompts" min={1} value={tpm} onChange={setTpm} />
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>
            Cancel
          </Button>
          <Button
            loading={busy}
            onClick={async () => {
              setBusy(true);
              try {
                await api.put("/rate-limits", {
                  scope,
                  requests_per_minute: rpm === "" ? null : Number(rpm),
                  tokens_per_minute: tpm === "" ? null : Number(tpm),
                });
                notifyOk("Rate limit saved");
                onSaved();
                onClose();
              } catch (e) {
                notifyError(e);
              } finally {
                setBusy(false);
              }
            }}
          >
            Save
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}
