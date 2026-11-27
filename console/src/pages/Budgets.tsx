import {
  Autocomplete,
  Badge,
  Button,
  Group,
  Modal,
  NumberInput,
  Progress,
  ScrollArea,
  Select,
  Stack,
  Switch,
  Table,
  Text,
} from "@mantine/core";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import {
  api,
  download,
  type Budget,
  type KeyView,
  type Organization,
  type RegisterEntry,
  type UserView,
} from "../api";
import { can, useMe } from "../App";
import {
  notifyError,
  notifyOk,
  PageHeader,
  QueryState,
  ReasonModal,
} from "../components/common";
import { money, pct } from "../format";
import { SafeDeleteModal } from "../components/SafeDelete";
import { RateLimitsSection } from "./RateLimits";

export function BudgetsPage() {
  const me = useMe().data;
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["budgets"],
    queryFn: () => api.get<Budget[]>("/budgets"),
  });
  const [edit, setEdit] = useState(false);
  const [editing, setEditing] = useState<Budget | null>(null);
  const [removing, setRemoving] = useState<Budget | null>(null);
  const [override, setOverride] = useState<Budget | null>(null);
  const [extra, setExtra] = useState<number | string>(10);

  return (
    <>
      <PageHeader
        title="Budgets"
        description="A spending limit either stops AI calls when reached or only sends an alert. Every AI call needs at least one limit that stops calls."
        actions={
          <Group>
            <Button
              variant="default"
              onClick={() =>
                download(
                  "/reports/chargeback?format=csv",
                  "captain-chargeback.csv",
                ).catch(notifyError)
              }
            >
              Chargeback CSV
            </Button>
            {can(me, "budget:write") && (
              <Button onClick={() => setEdit(true)}>Set budget</Button>
            )}
          </Group>
        }
      />
      <QueryState
        isLoading={q.isLoading}
        error={q.error}
        empty={q.data?.length === 0}
        emptyText="No budgets defined. With budgets required, all calls are refused until one exists."
      >
        <ScrollArea>
          <Table miw={860}>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Scope</Table.Th>
                <Table.Th>Period</Table.Th>
                <Table.Th>When reached</Table.Th>
                <Table.Th w={260}>Used</Table.Th>
                <Table.Th ta="right">Spent</Table.Th>
                <Table.Th ta="right">Reserved</Table.Th>
                <Table.Th ta="right">Limit</Table.Th>
                <Table.Th />
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {q.data?.map((b) => {
                const used = pct(
                  b.spent_micros + b.reserved_micros,
                  b.effective_limit_micros,
                );
                return (
                  <Table.Tr key={b.bucket}>
                    <Table.Td fw={600}>{b.scope}</Table.Td>
                    <Table.Td>{b.period}</Table.Td>
                    <Table.Td>
                      <Badge variant="light" color={b.hard ? "indigo" : "gray"}>
                        {b.hard ? "Stops AI calls" : "Alert only"}
                      </Badge>
                    </Table.Td>
                    <Table.Td>
                      <Progress
                        value={used}
                        color={
                          used >= 100 ? "red" : used >= 80 ? "yellow" : "teal"
                        }
                        aria-label={`${used}% used`}
                      />
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
                      <Group gap={4} wrap="nowrap">
                      {can(me, "budget:write") && (
                        <Button size="xs" variant="default" onClick={() => setEditing(b)}>
                          Edit
                        </Button>
                      )}
                      {can(me, "budget:override") && (
                        <Button
                          size="xs"
                          variant="light"
                          onClick={() => setOverride(b)}
                        >
                          Override
                        </Button>
                      )}
                      {can(me, "budget:write") && (
                        <Button size="xs" color="red" variant="light" onClick={() => setRemoving(b)}>
                          Delete
                        </Button>
                      )}
                      </Group>
                    </Table.Td>
                  </Table.Tr>
                );
              })}
            </Table.Tbody>
          </Table>
        </ScrollArea>
      </QueryState>
      <RateLimitsSection />
      <BudgetModal
        opened={edit}
        onClose={() => setEdit(false)}
        onSaved={() => qc.invalidateQueries({ queryKey: ["budgets"] })}
      />
      {editing && (
        <BudgetModal
          opened
          initial={editing}
          onClose={() => setEditing(null)}
          onSaved={() => qc.invalidateQueries({ queryKey: ["budgets"] })}
        />
      )}
      {removing && (
        <SafeDeleteModal
          what={`the spending limit for ${removing.scope}`}
          checkPath={`/budgets/delete-check?scope=${encodeURIComponent(removing.scope)}`}
          deletePath={`/budgets?scope=${encodeURIComponent(removing.scope)}`}
          effect="The limit is removed; spending already recorded stays in reports and the audit trail."
          details={(c) =>
            c.spent_micros ? (
              <Text size="sm">{money(c.spent_micros)} has been spent against it this period.</Text>
            ) : null
          }
          onClose={() => setRemoving(null)}
          onDeleted={() => qc.invalidateQueries({ queryKey: ["budgets"] })}
        />
      )}
      <ReasonModal
        opened={!!override}
        onClose={() => setOverride(null)}
        title={`Emergency override: ${override?.scope ?? ""}`}
        confirmLabel="Apply override"
        description={
          <Stack gap="xs">
            <Text size="sm">
              Raises this period's limit. The override and your reason are
              recorded and alerted.
            </Text>
            <NumberInput
              label="Extra (USD)"
              min={0.01}
              decimalScale={2}
              value={extra}
              onChange={setExtra}
            />
          </Stack>
        }
        onConfirm={async (reason) => {
          await api.post("/budgets/override", {
            scope: override!.scope,
            extra_micros: Math.round(Number(extra) * 1_000_000),
            reason,
          });
          notifyOk("Override applied");
          await qc.invalidateQueries({ queryKey: ["budgets"] });
        }}
      />
    </>
  );
}

const APPLIES = [
  { value: "org", label: "The whole company" },
  { value: "cost_centre", label: "A cost centre" },
  { value: "team", label: "A team" },
  { value: "project", label: "A team or project on keys" },
  { value: "principal", label: "One person or app" },
  { value: "model", label: "One model" },
];

export function BudgetModal({
  opened,
  onClose,
  onSaved,
  initial,
}: {
  opened: boolean;
  onClose: () => void;
  onSaved: () => void;
  initial?: Budget; // edit: what it applies to stays; period, limit and type change
}) {
  const org = useQuery({
    queryKey: ["organization"],
    queryFn: () => api.get<Organization>("/organization"),
    enabled: opened,
  });
  const reg = useQuery({
    queryKey: ["register"],
    queryFn: () => api.get<RegisterEntry[]>("/register"),
    enabled: opened,
  });
  const users = useQuery({
    queryKey: ["users"],
    queryFn: () => api.get<UserView[]>("/users"),
    enabled: opened,
  });
  const keys = useQuery({
    queryKey: ["keys"],
    queryFn: () => api.get<KeyView[]>("/keys"),
    enabled: opened,
  });
  const models = useQuery({
    queryKey: ["registry", "model"],
    queryFn: () => api.get<{ alias: string }[]>("/registry/model"),
    enabled: opened,
  });
  const [kind, setKind] = useState<string>(initial?.scope.split(":")[0] ?? "org");
  const [target, setTarget] = useState(
    initial ? initial.scope.slice(initial.scope.indexOf(":") + 1) : "",
  );
  const [period, setPeriod] = useState<string | null>(initial?.period ?? "MONTHLY");
  const [usd, setUsd] = useState<number | string>(
    initial ? initial.limit_micros / 1_000_000 : 100,
  );
  const [hard, setHard] = useState(initial?.hard ?? true);
  const [busy, setBusy] = useState(false);
  const uniq = (xs: (string | null | undefined)[]) =>
    [...new Set(xs.filter((x): x is string => !!x))].sort();
  const options: Record<string, string[]> = {
    cost_centre: uniq((reg.data ?? []).map((r) => r.cost_centre)),
    team: uniq((users.data ?? []).map((u) => u.team)),
    project: uniq((keys.data ?? []).map((k) => k.project_id)),
    principal: uniq([
      ...(users.data ?? []).map((u) => u.id),
      ...(keys.data ?? []).map((k) => k.principal_id),
    ]),
    model: uniq((models.data ?? []).map((m) => m.alias)),
  };
  const value = initial
    ? target
    : kind === "org"
      ? (org.data?.org_id ?? "default")
      : target.trim();
  return (
    <Modal
      opened={opened}
      onClose={onClose}
      title={initial ? `Edit the limit for ${initial.scope}` : "Set a budget"}
      centered
    >
      <Stack>
        {!initial && (
        <Select
          label="Applies to"
          description="A company-wide limit is the simplest start."
          data={APPLIES}
          value={kind}
          onChange={(v) => {
            setKind(v ?? "org");
            setTarget("");
          }}
          allowDeselect={false}
        />
        )}
        {!initial && kind !== "org" && (
          <Autocomplete
            label="Which one"
            placeholder="Pick or type"
            data={options[kind] ?? []}
            value={target}
            onChange={setTarget}
          />
        )}
        <Select
          label="Resets"
          data={[
            { value: "DAILY", label: "Every day" },
            { value: "MONTHLY", label: "Every month" },
          ]}
          value={period}
          onChange={setPeriod}
          allowDeselect={false}
        />
        <NumberInput
          label="Limit (US$)"
          min={0.01}
          decimalScale={2}
          value={usd}
          onChange={setUsd}
        />
        <Switch
          label="Stop AI calls when the limit is reached (otherwise only alert)"
          checked={hard}
          onChange={(e) => setHard(e.currentTarget.checked)}
        />
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>
            Cancel
          </Button>
          <Button
            loading={busy}
            disabled={!value || /\s/.test(value) || Number(usd) <= 0}
            onClick={async () => {
              setBusy(true);
              try {
                await api.put("/budgets", {
                  scope: `${kind}:${value}`,
                  period,
                  limit_micros: Math.round(Number(usd) * 1_000_000),
                  hard,
                });
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
