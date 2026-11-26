import { Alert, Button, Code, Group, List, NumberInput, Paper, ScrollArea, Select, SimpleGrid, Stack, Table, Text, Title } from "@mantine/core";
import { IconDownload, IconShieldCheck } from "@tabler/icons-react";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api, download, type EventItem } from "../api";
import { can, useMe } from "../App";
import { notifyError, PageHeader, QueryState } from "../components/common";
import { when } from "../format";

interface Verify {
  ok: boolean;
  entries_checked: number;
  anchors_checked: number;
  problems: string[];
}

export function AuditPage() {
  const me = useMe().data;
  const [verify, setVerify] = useState<Verify | null>(null);
  const [busy, setBusy] = useState(false);
  const [framework, setFramework] = useState<string | null>("EU_AI_ACT");
  const [days, setDays] = useState<number | string>(30);
  const now = new Date();
  const [ym, setYm] = useState(`${now.getUTCFullYear()}-${now.getUTCMonth() + 1}`);
  const admin = useQuery({ queryKey: ["admin-events"], queryFn: () => api.get<{ items: EventItem[] }>("/events?kind=admin&days=30&limit=100") });

  return (
    <>
      <PageHeader title="Audit & reports" description="Verify the tamper-evident evidence ledger, export control-mapped evidence for auditors, and download reports." />
      <SimpleGrid cols={{ base: 1, md: 3 }} mb="lg">
        <Paper withBorder p="md">
          <Title order={4}>Ledger integrity</Title>
          <Text size="sm" c="dimmed" mb="sm">
            Recomputes every hash, checks every signature and every write-once anchor.
          </Text>
          <Button
            leftSection={<IconShieldCheck size={16} />}
            loading={busy}
            onClick={async () => {
              setBusy(true);
              try {
                setVerify(await api.post<Verify>("/ledger/verify"));
              } catch (e) {
                notifyError(e);
              } finally {
                setBusy(false);
              }
            }}
          >
            Verify now
          </Button>
          {verify && (
            <Alert mt="sm" color={verify.ok ? "teal" : "red"} title={verify.ok ? "Ledger verified" : "Integrity problem found"}>
              {verify.entries_checked} entries, {verify.anchors_checked} anchors checked.
              {!verify.ok && (
                <List size="xs" mt={4}>
                  {verify.problems.slice(0, 5).map((p) => (
                    <List.Item key={p}>{p}</List.Item>
                  ))}
                </List>
              )}
            </Alert>
          )}
        </Paper>
        <Paper withBorder p="md">
          <Title order={4}>Evidence export</Title>
          <Text size="sm" c="dimmed" mb="sm">
            Zip with per-control counts, the signed ledger entries and a manifest an auditor can verify independently.
          </Text>
          <Stack gap="xs">
            <Select data={["EU_AI_ACT", "ISO_42001", "NIST_AI_RMF"]} value={framework} onChange={setFramework} aria-label="Framework" />
            <NumberInput label="Days" min={1} max={3660} value={days} onChange={setDays} />
            <Button
              leftSection={<IconDownload size={16} />}
              disabled={!can(me, "audit:export")}
              onClick={() => download(`/evidence?framework=${framework}&days=${days}`, "captain-evidence.zip").catch(notifyError)}
            >
              Download evidence
            </Button>
          </Stack>
        </Paper>
        <Paper withBorder p="md">
          <Title order={4}>Monthly report</Title>
          <Text size="sm" c="dimmed" mb="sm">
            Usage, spend, refusals, incidents, approvals and top-risk systems: board-pack inputs.
          </Text>
          <Stack gap="xs">
            <Select
              aria-label="Month"
              value={ym}
              onChange={(v) => setYm(v ?? ym)}
              data={Array.from({ length: 6 }, (_, i) => {
                const d = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() - i, 1));
                return { value: `${d.getUTCFullYear()}-${d.getUTCMonth() + 1}`, label: d.toLocaleDateString(undefined, { year: "numeric", month: "long" }) };
              })}
            />
            <Group grow>
              <Button
                leftSection={<IconDownload size={16} />}
                onClick={() => {
                  const [y, m] = ym.split("-");
                  download(`/reports/monthly?year=${y}&month=${m}&format=xlsx`, "captain-report.xlsx").catch(notifyError);
                }}
              >
                XLSX
              </Button>
            </Group>
          </Stack>
        </Paper>
      </SimpleGrid>

      <Title order={4} mb="sm">
        Administrative changes (last 30 days)
      </Title>
      <QueryState isLoading={admin.isLoading} error={admin.error} empty={admin.data?.items.length === 0} emptyText="No administrative changes.">
        <ScrollArea>
          <Table striped miw={800}>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>When</Table.Th>
                <Table.Th>Change</Table.Th>
                <Table.Th>By</Table.Th>
                <Table.Th>Details</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {admin.data?.items.map((e) => (
                <Table.Tr key={e.event_id}>
                  <Table.Td>{when(e.recorded_at)}</Table.Td>
                  <Table.Td>
                    <Code>{e.event_type}</Code>
                  </Table.Td>
                  <Table.Td>{e.principal_id}</Table.Td>
                  <Table.Td>
                    <Text size="xs">
                      {Object.entries(e.details ?? {})
                        .filter(([, v]) => v !== null && v !== "")
                        .map(([k, v]) => `${k}: ${String(v)}`)
                        .join(" · ")}
                    </Text>
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </ScrollArea>
      </QueryState>
    </>
  );
}
