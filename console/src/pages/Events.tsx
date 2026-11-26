import { Accordion, Badge, Button, Drawer, Group, Pagination, ScrollArea, Select, Stack, Table, Text, Title } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api, type EventItem } from "../api";
import { PageHeader, QueryState } from "../components/common";
import { money, when } from "../format";

const PAGE = 25;

// Result filter in plain words -> the decision Captain recorded.
const RESULTS = [
  { value: "ALLOW", label: "Allowed" },
  { value: "ALLOW_WITH_REDACTION", label: "Allowed, data hidden" },
  { value: "ALLOW_WITH_LIMITS", label: "Allowed with limits" },
  { value: "REQUIRE_APPROVAL", label: "Waiting for approval" },
  { value: "BLOCK", label: "Blocked" },
];

export function OutcomeBadge({ e }: { e: Pick<EventItem, "outcome" | "outcome_tone"> }) {
  return (
    <Badge color={e.outcome_tone || "gray"} variant="light" miw="max-content">
      {e.outcome}
    </Badge>
  );
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
        description="What happened with AI in your organisation: who used it, what Captain decided and why, and what it cost. The text people typed and the AI's answers are never stored or shown here."
        actions={
          <Group>
            <Button component="a" variant="default" download href={`/admin/v1/events.csv?days=${days}${decision ? `&decision=${decision}` : ""}`}>
              Download CSV
            </Button>
            <Select
              aria-label="Result"
              placeholder="All results"
              clearable
              value={decision}
              onChange={(v) => {
                setDecision(v);
                setPage(1);
              }}
              data={RESULTS}
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
      <QueryState isLoading={q.isLoading} error={q.error} empty={q.data?.total === 0} emptyText="Nothing happened in this period.">
        <ScrollArea>
          <Table striped highlightOnHover miw={900}>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>When</Table.Th>
                <Table.Th>Who</Table.Th>
                <Table.Th>What happened</Table.Th>
                <Table.Th>Result</Table.Th>
                <Table.Th>AI system</Table.Th>
                <Table.Th ta="right">Cost</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {q.data?.items.map((e) => (
                <Table.Tr key={e.event_id} onClick={() => e.request_id && setOpen(e.request_id)} style={{ cursor: e.request_id ? "pointer" : undefined }}>
                  <Table.Td miw={150}>{when(e.recorded_at)}</Table.Td>
                  <Table.Td>{e.who}</Table.Td>
                  <Table.Td>
                    <Text size="sm">{e.headline}</Text>
                    {e.why && (
                      <Text size="xs" c="dimmed">
                        {e.why}
                      </Text>
                    )}
                  </Table.Td>
                  <Table.Td>
                    <OutcomeBadge e={e} />
                  </Table.Td>
                  <Table.Td>{e.register_id}</Table.Td>
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
    <Drawer opened={!!id} onClose={onClose} title="What happened with this request" position="right" size="lg">
      <QueryState isLoading={q.isLoading} error={q.error}>
        <Stack>
          {q.data?.events.map((e) => (
            <Stack key={e.event_id} gap={6}>
              <Group justify="space-between" wrap="nowrap" align="flex-start">
                <Title order={5}>{e.headline}</Title>
                <OutcomeBadge e={e} />
              </Group>
              {e.why && <Text size="sm">{e.why}</Text>}
              <Text size="xs" c="dimmed">
                {when(e.recorded_at)} · {e.category}
                {e.cost_micros != null ? ` · ${money(e.cost_micros)}` : ""}
              </Text>
              <TechnicalDetails e={e} />
            </Stack>
          ))}
          <Text size="xs" c="dimmed">
            Request {id}
          </Text>
        </Stack>
      </QueryState>
    </Drawer>
  );
}

// The raw evidence, for administrators and auditors. Collapsed by default.
export function TechnicalDetails({ e }: { e: EventItem }) {
  const detected = typeof e.details?.detected === "string" ? e.details.detected : null;
  const rows = (
    [
      ["Event type", e.event_type],
      ["Decision", e.decision],
      ["Reason code", e.reason_code],
      ["Who (id)", e.principal_id],
      ["AI system", e.register_id],
      ["Model / tool", e.model ?? e.tool],
      ["Channel / coverage", `${e.channel ?? ""} / ${e.coverage ?? ""}`],
      ["Found in the request", detected],
      ["Policy", e.policy_version],
      ["Risk", e.risk_level ? `${e.risk_level} (${e.risk_score})` : null],
      ["Tokens in / out", e.input_tokens != null ? `${e.input_tokens} / ${e.output_tokens}` : null],
      ["Cost", e.cost_micros != null ? `${money(e.cost_micros)} (${e.usage_source})` : null],
      ["Latency", e.latency_ms != null ? `${e.latency_ms} ms` : null],
      ["Evidence record", `${e.partition} #${e.seq}${e.anchor_id ? ` · anchored ${e.anchor_id}` : " · not yet anchored"}`],
    ] as [string, string | null][]
  ).filter(([, v]) => v && v !== " / ");
  return (
    <Accordion variant="contained" chevronPosition="left">
      <Accordion.Item value="tech">
        <Accordion.Control>
          <Text size="sm">Technical details</Text>
        </Accordion.Control>
        <Accordion.Panel>
          <Table>
            <Table.Tbody>
              {rows.map(([k, v]) => (
                <Table.Tr key={k}>
                  <Table.Td c="dimmed" w={170}>
                    {k}
                  </Table.Td>
                  <Table.Td style={{ wordBreak: "break-word" }}>{v}</Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </Accordion.Panel>
      </Accordion.Item>
    </Accordion>
  );
}
