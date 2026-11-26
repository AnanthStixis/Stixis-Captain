import { Button, Code, Drawer, Group, Pagination, ScrollArea, Select, Stack, Table, Text, Title } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api, type EventItem } from "../api";
import { DecisionBadge, PageHeader, QueryState, RiskBadge } from "../components/common";
import { money, when } from "../format";

const PAGE = 25;

// What the guardrails found (kinds and counts only, never the values).
function detected(e: EventItem): string | null {
  const d = e.details?.detected;
  return typeof d === "string" && d ? d : null;
}

export function EventsPage() {
  const [days, setDays] = useState("7");
  const [decision, setDecision] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [open, setOpen] = useState<string | null>(null);
  const qs = new URLSearchParams({ days, limit: String(PAGE), offset: String((page - 1) * PAGE) });
  if (decision) qs.set("decision", decision);
  const q = useQuery({ queryKey: ["events", days, decision, page], queryFn: () => api.get<{ total: number; items: EventItem[] }>(`/events?${qs}`) });

  return (
    <>
      <PageHeader
        title="Activity"
        description="Every governed decision and settled call. Prompt and response content is never stored or shown."
        actions={
          <Group>
            <Button component="a" variant="default" download href={`/admin/v1/events.csv?days=${days}${decision ? `&decision=${decision}` : ""}`}>
              Download CSV
            </Button>
            <Select
              aria-label="Decision"
              placeholder="All decisions"
              clearable
              value={decision}
              onChange={(v) => {
                setDecision(v);
                setPage(1);
              }}
              data={["ALLOW", "ALLOW_WITH_LIMITS", "ALLOW_WITH_REDACTION", "REQUIRE_APPROVAL", "BLOCK"]}
              w={220}
            />
            <Select
              aria-label="Period"
              value={days}
              onChange={(v) => {
                setDays(v ?? "7");
                setPage(1);
              }}
              data={[
                { value: "1", label: "Last 24 hours" },
                { value: "7", label: "Last 7 days" },
                { value: "30", label: "Last 30 days" },
              ]}
              w={170}
            />
          </Group>
        }
      />
      <QueryState isLoading={q.isLoading} error={q.error} empty={q.data?.total === 0} emptyText="No activity in this period.">
        <ScrollArea>
          <Table striped highlightOnHover miw={900}>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Time</Table.Th>
                <Table.Th>Event</Table.Th>
                <Table.Th>Decision</Table.Th>
                <Table.Th>Reason</Table.Th>
                <Table.Th>System</Table.Th>
                <Table.Th>Model / tool</Table.Th>
                <Table.Th>Risk</Table.Th>
                <Table.Th ta="right">Cost</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {q.data?.items.map((e) => (
                <Table.Tr key={e.event_id} onClick={() => e.request_id && setOpen(e.request_id)} style={{ cursor: e.request_id ? "pointer" : undefined }}>
                  <Table.Td>{when(e.recorded_at)}</Table.Td>
                  <Table.Td>
                    <Code>{e.event_type}</Code>
                  </Table.Td>
                  <Table.Td>
                    <DecisionBadge value={e.decision} />
                  </Table.Td>
                  <Table.Td>
                    {e.reason_code?.replaceAll("_", " ") ?? ""}
                    {detected(e) && (
                      <Text size="xs" c="dimmed">
                        found: {detected(e)}
                      </Text>
                    )}
                  </Table.Td>
                  <Table.Td>{e.register_id}</Table.Td>
                  <Table.Td>{e.model ?? e.tool ?? ""}</Table.Td>
                  <Table.Td>
                    <RiskBadge value={e.risk_level} />
                  </Table.Td>
                  <Table.Td ta="right">
                    {e.cost_micros != null ? money(e.cost_micros) : ""}
                    {e.usage_source && e.usage_source !== "PROVIDER_REPORTED" ? " (est.)" : ""}
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </ScrollArea>
        <Group justify="center" mt="md">
          <Pagination value={page} onChange={setPage} total={Math.max(1, Math.ceil((q.data?.total ?? 0) / PAGE))} />
        </Group>
      </QueryState>
      <RequestDrawer id={open} onClose={() => setOpen(null)} />
    </>
  );
}

function RequestDrawer({ id, onClose }: { id: string | null; onClose: () => void }) {
  const q = useQuery({ queryKey: ["request", id], queryFn: () => api.get<{ events: EventItem[] }>(`/requests/${id}`), enabled: !!id });
  return (
    <Drawer opened={!!id} onClose={onClose} title="Request detail" position="right" size="lg">
      <QueryState isLoading={q.isLoading} error={q.error}>
        <Stack>
          <Text size="sm" c="dimmed">
            Request {id}
          </Text>
          {q.data?.events.map((e) => (
            <Stack key={e.event_id} gap={4}>
              <Group justify="space-between">
                <Title order={5}>{e.event_type}</Title>
                <DecisionBadge value={e.decision} />
              </Group>
              <Table>
                <Table.Tbody>
                  {(
                    [
                      ["Time", when(e.recorded_at)],
                      ["Principal", e.principal_id],
                      ["System", e.register_id],
                      ["Channel / coverage", `${e.channel ?? ""} / ${e.coverage ?? ""}`],
                      ["Reason", e.reason_code],
                      ["Found in the request", detected(e)],
                      ["Policy", e.policy_version],
                      ["Risk", e.risk_level ? `${e.risk_level} (${e.risk_score})` : null],
                      ["Tokens in / out", e.input_tokens != null ? `${e.input_tokens} / ${e.output_tokens}` : null],
                      ["Cost", e.cost_micros != null ? `${money(e.cost_micros)} (${e.usage_source})` : null],
                      ["Latency", e.latency_ms != null ? `${e.latency_ms} ms` : null],
                      ["Ledger", `${e.partition} #${e.seq}${e.anchor_id ? ` · anchored ${e.anchor_id}` : " · not yet anchored"}`],
                    ] as [string, string | null][]
                  )
                    .filter(([, v]) => v)
                    .map(([k, v]) => (
                      <Table.Tr key={k}>
                        <Table.Td c="dimmed" w={160}>
                          {k}
                        </Table.Td>
                        <Table.Td>{v}</Table.Td>
                      </Table.Tr>
                    ))}
                </Table.Tbody>
              </Table>
            </Stack>
          ))}
        </Stack>
      </QueryState>
    </Drawer>
  );
}
