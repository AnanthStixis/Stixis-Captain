import { Alert, Badge, Button, Group, Modal, ScrollArea, Select, Stack, Switch, Table, Text, TextInput } from "@mantine/core";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, ApiError, type RegisterEntry } from "../api";
import { can, useMe } from "../App";
import { notifyError, notifyOk, PageHeader, QueryState } from "../components/common";

const TIER_COLOR: Record<string, string> = { TIER_1: "red", TIER_2: "orange", TIER_3: "yellow", TIER_4: "gray" };

export function RegisterPage() {
  const me = useMe().data;
  const q = useQuery({ queryKey: ["register"], queryFn: () => api.get<RegisterEntry[]>("/register") });
  const [edit, setEdit] = useState<RegisterEntry | null>(null);
  return (
    <>
      <PageHeader
        title="AI Register"
        description="Every AI system with its owner, purpose, risk tier and autonomy level. Policy, guardrails and approvals follow the tier. Weakening controls needs an approval reference."
      />
      <QueryState isLoading={q.isLoading} error={q.error} empty={q.data?.length === 0} emptyText="No AI systems registered.">
        <ScrollArea>
          <Table striped miw={900}>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>System</Table.Th>
                <Table.Th>Tier</Table.Th>
                <Table.Th>Autonomy</Table.Th>
                <Table.Th>Owner / cost centre</Table.Th>
                <Table.Th>Allowed models</Table.Th>
                <Table.Th>Allowed tools</Table.Th>
                <Table.Th>Status</Table.Th>
                <Table.Th />
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {q.data?.map((e) => (
                <Table.Tr key={e.id}>
                  <Table.Td>
                    <Text fw={600}>{e.name}</Text>
                    <Text size="xs" c="dimmed">
                      {e.id} · v{e.version} · {e.purpose}
                    </Text>
                  </Table.Td>
                  <Table.Td>
                    <Badge miw="max-content" color={TIER_COLOR[e.risk_tier]}>{e.risk_tier.replace("_", " ")}</Badge>
                    {e.eu_ai_act_high_risk && (
                      <Badge ml={4} size="xs" color="red" variant="outline">
                        EU high-risk
                      </Badge>
                    )}
                  </Table.Td>
                  <Table.Td>{e.autonomy}</Table.Td>
                  <Table.Td>
                    {e.owner_id}
                    <Text size="xs" c="dimmed">
                      {e.cost_centre}
                    </Text>
                  </Table.Td>
                  <Table.Td>{e.allowed_models.join(", ") || "—"}</Table.Td>
                  <Table.Td>{e.allowed_tools.join(", ") || "—"}</Table.Td>
                  <Table.Td>{e.active ? <Badge miw="max-content" color="teal" variant="light">active</Badge> : <Badge miw="max-content" color="gray">retired</Badge>}</Table.Td>
                  <Table.Td>
                    {can(me, "register:write") && (
                      <Button size="xs" variant="default" onClick={() => setEdit(e)}>
                        Edit
                      </Button>
                    )}
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </ScrollArea>
      </QueryState>
      {edit && <EditModal entry={edit} onClose={() => setEdit(null)} />}
    </>
  );
}

function EditModal({ entry, onClose }: { entry: RegisterEntry; onClose: () => void }) {
  const qc = useQueryClient();
  const [e, setE] = useState(entry);
  const [models, setModels] = useState(entry.allowed_models.join(", "));
  const [approval, setApproval] = useState("");
  const [needsApproval, setNeedsApproval] = useState(false);
  const [busy, setBusy] = useState(false);
  const list = (s: string) => s.split(",").map((x) => x.trim()).filter(Boolean);
  return (
    <Modal opened onClose={onClose} title={`Edit ${entry.id}`} centered size="lg">
      <Stack>
        <Group grow>
          <Select label="Risk tier" data={["TIER_1", "TIER_2", "TIER_3", "TIER_4"]} value={e.risk_tier} onChange={(v) => setE({ ...e, risk_tier: v ?? e.risk_tier })} />
          <Select label="Autonomy" data={["A0", "A1", "A2", "A3", "A4"]} value={e.autonomy} onChange={(v) => setE({ ...e, autonomy: v ?? e.autonomy })} />
        </Group>
        <TextInput label="Allowed models (comma separated)" value={models} onChange={(x) => setModels(x.currentTarget.value)} />
        <Switch label="EU AI Act high-risk (requires Tier 1 or 2)" checked={e.eu_ai_act_high_risk} onChange={(x) => setE({ ...e, eu_ai_act_high_risk: x.currentTarget.checked })} />
        <Switch label="Active" checked={e.active} onChange={(x) => setE({ ...e, active: x.currentTarget.checked })} />
        {needsApproval && <Alert color="yellow">This change weakens controls (higher tier number, lower autonomy or removed high-risk flag). Enter the approval reference.</Alert>}
        <TextInput label="Approval reference (needed only when weakening controls)" value={approval} onChange={(x) => setApproval(x.currentTarget.value)} />
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>
            Cancel
          </Button>
          <Button
            loading={busy}
            onClick={async () => {
              setBusy(true);
              try {
                await api.put(`/register/${entry.id}`, {
                  entry: { ...e, allowed_models: list(models), allowed_tools: e.allowed_tools },
                  approval_ref: approval.trim() || null,
                });
                notifyOk("Register entry saved");
                await qc.invalidateQueries({ queryKey: ["register"] });
                onClose();
              } catch (err) {
                if (err instanceof ApiError && err.code === "APPROVAL_REQUIRED") setNeedsApproval(true);
                else notifyError(err);
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
