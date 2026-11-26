import { Badge, Button, Group, Modal, NumberInput, Progress, ScrollArea, Select, Stack, Switch, Table, Text, TextInput } from "@mantine/core";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, download, type Budget } from "../api";
import { can, useMe } from "../App";
import { notifyError, notifyOk, PageHeader, QueryState, ReasonModal } from "../components/common";
import { money, pct } from "../format";
import { RateLimitsSection } from "./RateLimits";

export function BudgetsPage() {
  const me = useMe().data;
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["budgets"], queryFn: () => api.get<Budget[]>("/budgets") });
  const [edit, setEdit] = useState(false);
  const [override, setOverride] = useState<Budget | null>(null);
  const [extra, setExtra] = useState<number | string>(10);

  return (
    <>
      <PageHeader
        title="Budgets"
        description="Hard budgets refuse calls before they spend; soft budgets only alert. Every call must be covered by at least one hard budget."
        actions={
          <Group>
            <Button variant="default" onClick={() => download("/reports/chargeback?format=csv", "captain-chargeback.csv").catch(notifyError)}>
              Chargeback CSV
            </Button>
            {can(me, "budget:write") && <Button onClick={() => setEdit(true)}>Set budget</Button>}
          </Group>
        }
      />
      <QueryState isLoading={q.isLoading} error={q.error} empty={q.data?.length === 0} emptyText="No budgets defined. With budgets required, all calls are refused until one exists.">
        <ScrollArea>
          <Table miw={860}>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Scope</Table.Th>
                <Table.Th>Period</Table.Th>
                <Table.Th>Type</Table.Th>
                <Table.Th w={260}>Used</Table.Th>
                <Table.Th ta="right">Spent</Table.Th>
                <Table.Th ta="right">Reserved</Table.Th>
                <Table.Th ta="right">Limit</Table.Th>
                <Table.Th />
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {q.data?.map((b) => {
                const used = pct(b.spent_micros + b.reserved_micros, b.effective_limit_micros);
                return (
                  <Table.Tr key={b.bucket}>
                    <Table.Td fw={600}>{b.scope}</Table.Td>
                    <Table.Td>{b.period}</Table.Td>
                    <Table.Td>
                      <Badge variant="light" color={b.hard ? "indigo" : "gray"}>
                        {b.hard ? "hard" : "soft"}
                      </Badge>
                    </Table.Td>
                    <Table.Td>
                      <Progress value={used} color={used >= 100 ? "red" : used >= 80 ? "yellow" : "teal"} aria-label={`${used}% used`} />
                      <Text size="xs" c="dimmed">
                        {used}%
                      </Text>
                    </Table.Td>
                    <Table.Td ta="right">{money(b.spent_micros)}</Table.Td>
                    <Table.Td ta="right">{money(b.reserved_micros)}</Table.Td>
                    <Table.Td ta="right">
                      {money(b.effective_limit_micros)}
                      {b.effective_limit_micros > b.limit_micros && (
                        <Text size="xs" c="yellow.7">
                          incl. override
                        </Text>
                      )}
                    </Table.Td>
                    <Table.Td>
                      {can(me, "budget:override") && (
                        <Button size="xs" variant="light" onClick={() => setOverride(b)}>
                          Override
                        </Button>
                      )}
                    </Table.Td>
                  </Table.Tr>
                );
              })}
            </Table.Tbody>
          </Table>
        </ScrollArea>
      </QueryState>
      <RateLimitsSection />
      <BudgetModal opened={edit} onClose={() => setEdit(false)} onSaved={() => qc.invalidateQueries({ queryKey: ["budgets"] })} />
      <ReasonModal
        opened={!!override}
        onClose={() => setOverride(null)}
        title={`Emergency override: ${override?.scope ?? ""}`}
        confirmLabel="Apply override"
        description={
          <Stack gap="xs">
            <Text size="sm">Raises this period's limit. The override and your reason are recorded and alerted.</Text>
            <NumberInput label="Extra (USD)" min={0.01} decimalScale={2} value={extra} onChange={setExtra} />
          </Stack>
        }
        onConfirm={async (reason) => {
          await api.post("/budgets/override", { scope: override!.scope, extra_micros: Math.round(Number(extra) * 1_000_000), reason });
          notifyOk("Override applied");
          await qc.invalidateQueries({ queryKey: ["budgets"] });
        }}
      />
    </>
  );
}

function BudgetModal({ opened, onClose, onSaved }: { opened: boolean; onClose: () => void; onSaved: () => void }) {
  const [scope, setScope] = useState("org:");
  const [period, setPeriod] = useState<string | null>("MONTHLY");
  const [usd, setUsd] = useState<number | string>(100);
  const [hard, setHard] = useState(true);
  const [busy, setBusy] = useState(false);
  return (
    <Modal opened={opened} onClose={onClose} title="Set a budget" centered>
      <Stack>
        <TextInput
          label="Scope"
          description="org:<id>, cost_centre:<id>, team:<id>, project:<id>, app:<id>, principal:<id>, key:<id> or model:<alias>"
          value={scope}
          onChange={(e) => setScope(e.currentTarget.value)}
        />
        <Select label="Period" data={["DAILY", "MONTHLY"]} value={period} onChange={setPeriod} />
        <NumberInput label="Limit (USD)" min={0.01} decimalScale={2} value={usd} onChange={setUsd} />
        <Switch label="Hard limit (refuse calls over the limit)" checked={hard} onChange={(e) => setHard(e.currentTarget.checked)} />
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>
            Cancel
          </Button>
          <Button
            loading={busy}
            onClick={async () => {
              setBusy(true);
              try {
                await api.put("/budgets", { scope, period, limit_micros: Math.round(Number(usd) * 1_000_000), hard });
                notifyOk("Budget saved");
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
