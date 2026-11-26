import { Alert, Badge, Button, Group, Paper, Select, Stack, Table, Text, TextInput } from "@mantine/core";
import { IconPower } from "@tabler/icons-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, type KillEntry } from "../api";
import { can, useMe } from "../App";
import { notifyOk, PageHeader, QueryState, ReasonModal } from "../components/common";
import { when } from "../format";

const SCOPES = [
  { value: "org", label: "Entire organisation" },
  { value: "register", label: "AI system (register entry)" },
  { value: "application", label: "Application" },
  { value: "project", label: "Project" },
  { value: "principal", label: "User / agent / service" },
  { value: "key", label: "Virtual key" },
  { value: "model", label: "Model" },
  { value: "tool", label: "Tool" },
  { value: "mcp_server", label: "MCP server" },
  { value: "channel", label: "Channel (API, MCP, IDE, BROWSER, …)" },
];

export function KillSwitchPage() {
  const me = useMe().data;
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["killswitch"], queryFn: () => api.get<KillEntry[]>("/killswitch") });
  const [scope, setScope] = useState<string | null>("model");
  const [target, setTarget] = useState("");
  const [confirm, setConfirm] = useState<"on" | KillEntry | null>(null);
  const write = can(me, "killswitch:write");

  return (
    <>
      <PageHeader title="Kill switch" description="Stops new calls immediately on every Captain instance, cancels pending approvals for a principal, and revokes a key." />
      {write && (
        <Paper withBorder p="md" mb="lg">
          <Group align="flex-end" wrap="wrap">
            <Select label="Scope" data={SCOPES} value={scope} onChange={setScope} w={260} />
            <TextInput label="Target" placeholder="e.g. claude-sonnet, AI-0042, org-1" value={target} onChange={(e) => setTarget(e.currentTarget.value)} w={260} />
            <Button color="red" leftSection={<IconPower size={16} />} disabled={!scope || !target.trim()} onClick={() => setConfirm("on")}>
              Activate
            </Button>
          </Group>
        </Paper>
      )}
      <QueryState isLoading={q.isLoading} error={q.error} empty={q.data?.length === 0} emptyText="No kill switches active.">
        <Alert color="red" mb="md" title={`${q.data?.length ?? 0} active`}>
          Matching calls are refused with KILL_SWITCH_ACTIVE.
        </Alert>
        <Table>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Scope</Table.Th>
              <Table.Th>Target</Table.Th>
              <Table.Th>Reason</Table.Th>
              <Table.Th>By</Table.Th>
              <Table.Th>Since</Table.Th>
              <Table.Th />
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {q.data?.map((k) => (
              <Table.Tr key={`${k.scope}:${k.target}`}>
                <Table.Td>
                  <Badge color="red" variant="light">
                    {k.scope}
                  </Badge>
                </Table.Td>
                <Table.Td fw={600}>{k.target}</Table.Td>
                <Table.Td>{k.reason}</Table.Td>
                <Table.Td>{k.actor_id}</Table.Td>
                <Table.Td>{when(k.activated_at)}</Table.Td>
                <Table.Td>
                  {write && (
                    <Button size="xs" variant="default" onClick={() => setConfirm(k)}>
                      Release
                    </Button>
                  )}
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      </QueryState>
      <ReasonModal
        opened={confirm !== null}
        onClose={() => setConfirm(null)}
        danger={confirm === "on"}
        title={confirm === "on" ? "Activate kill switch" : "Release kill switch"}
        confirmLabel={confirm === "on" ? "Activate now" : "Release"}
        description={
          <Stack gap={4}>
            <Text size="sm">
              {confirm === "on" ? `Stop all calls for ${scope} "${target}".` : `Allow calls for ${(confirm as KillEntry)?.scope} "${(confirm as KillEntry)?.target}" again.`}
            </Text>
            {confirm !== "on" && <Text size="xs" c="dimmed">Releasing is recorded before it takes effect; if evidence cannot be written it is refused.</Text>}
          </Stack>
        }
        onConfirm={async (reason) => {
          if (confirm === "on") await api.post("/killswitch", { scope, target: target.trim(), reason });
          else if (confirm) await api.post("/killswitch/release", { scope: confirm.scope, target: confirm.target, reason });
          notifyOk(confirm === "on" ? "Kill switch active" : "Kill switch released");
          setTarget("");
          await qc.invalidateQueries({ queryKey: ["killswitch"] });
        }}
      />
    </>
  );
}
