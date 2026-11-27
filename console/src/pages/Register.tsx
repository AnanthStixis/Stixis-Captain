import {
  Alert,
  Badge,
  Button,
  Group,
  Modal,
  ScrollArea,
  Select,
  Stack,
  Switch,
  Table,
  TagsInput,
  Text,
  Textarea,
  TextInput,
} from "@mantine/core";
import { IconPlus } from "@tabler/icons-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, ApiError, type RegisterEntry, type UserView } from "../api";
import { can, useMe } from "../App";
import {
  notifyError,
  notifyOk,
  PageHeader,
  QueryState,
} from "../components/common";

const TIER_COLOR: Record<string, string> = {
  TIER_1: "red",
  TIER_2: "orange",
  TIER_3: "yellow",
  TIER_4: "gray",
};

/** Risk levels and autonomy in plain words (the checks follow policies/captain/data.json). */
const TIERS = [
  {
    value: "TIER_1",
    label: "Critical",
    help: "Strictest checks; every agent action needs approval. Regulated or customer-impacting uses.",
  },
  {
    value: "TIER_2",
    label: "High",
    help: "Blocks secrets and prompt attacks, hides personal data, agent actions need approval.",
  },
  {
    value: "TIER_3",
    label: "Medium (recommended)",
    help: "Hides personal data, records secrets and prompt attacks; risky agent actions need approval.",
  },
  {
    value: "TIER_4",
    label: "Low",
    help: "Light checks, for experiments with no sensitive data.",
  },
];
const AUTONOMY = [
  { value: "A0", label: "Answers questions only" },
  { value: "A1", label: "Suggests; a person does the action" },
  { value: "A2", label: "Does low-risk steps itself" },
  { value: "A3", label: "Acts on its own; a person can step in" },
  { value: "A4", label: "Fully autonomous agent" },
];
const TIER_NAME: Record<string, string> = Object.fromEntries(
  TIERS.map((t) => [t.value, t.label.replace(" (recommended)", "")]),
);
const AUTONOMY_NAME: Record<string, string> = Object.fromEntries(
  AUTONOMY.map((t) => [t.value, t.label]),
);

export function RegisterPage() {
  const me = useMe().data;
  const q = useQuery({
    queryKey: ["register"],
    queryFn: () => api.get<RegisterEntry[]>("/register"),
  });
  const [edit, setEdit] = useState<RegisterEntry | "new" | null>(null);
  const write = can(me, "register:write");
  return (
    <>
      <PageHeader
        title="AI Register"
        description="The approved ways your company uses AI (for example a coding assistant or a support chatbot). Each has an owner, a risk level and the models it may use; Captain applies the matching checks to every key that belongs to it."
        actions={
          write && (
            <Button
              leftSection={<IconPlus size={16} />}
              onClick={() => setEdit("new")}
            >
              Add an AI use
            </Button>
          )
        }
      />
      <QueryState
        isLoading={q.isLoading}
        error={q.error}
        empty={q.data?.length === 0}
        emptyText="Nothing yet. Press Add an AI use (for example: Coding assistant)."
      >
        <ScrollArea>
          <Table striped miw={900}>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>AI use</Table.Th>
                <Table.Th>Risk level</Table.Th>
                <Table.Th>What it may do</Table.Th>
                <Table.Th>Owner / cost centre</Table.Th>
                <Table.Th>Models</Table.Th>
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
                      {e.purpose}
                    </Text>
                  </Table.Td>
                  <Table.Td>
                    <Badge
                      miw="max-content"
                      color={TIER_COLOR[e.risk_tier]}
                      style={{ textTransform: "none" }}
                    >
                      {TIER_NAME[e.risk_tier] ?? e.risk_tier}
                    </Badge>
                    {e.eu_ai_act_high_risk && (
                      <Badge ml={4} size="xs" color="red" variant="outline">
                        EU high-risk
                      </Badge>
                    )}
                  </Table.Td>
                  <Table.Td>{AUTONOMY_NAME[e.autonomy] ?? e.autonomy}</Table.Td>
                  <Table.Td>
                    {e.owner_id}
                    <Text size="xs" c="dimmed">
                      {e.cost_centre}
                    </Text>
                  </Table.Td>
                  <Table.Td>{e.allowed_models.join(", ") || "—"}</Table.Td>
                  <Table.Td>
                    {e.active ? (
                      <Badge miw="max-content" color="teal" variant="light">
                        Active
                      </Badge>
                    ) : (
                      <Badge miw="max-content" color="gray">
                        Retired
                      </Badge>
                    )}
                  </Table.Td>
                  <Table.Td>
                    {write && (
                      <Button
                        size="xs"
                        variant="default"
                        onClick={() => setEdit(e)}
                      >
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
      {edit && (
        <EntryModal
          entry={edit === "new" ? null : edit}
          taken={(q.data ?? []).map((x) => x.id)}
          onClose={() => setEdit(null)}
        />
      )}
    </>
  );
}

function newId(name: string, taken: string[]): string {
  const base =
    "AI-" +
    (name
      .toUpperCase()
      .replace(/[^A-Z0-9]+/g, "-")
      .replace(/^-|-$/g, "")
      .slice(0, 24) || "USE");
  let id = base;
  for (let i = 2; taken.includes(id); i++) id = `${base}-${i}`;
  return id;
}

export function EntryModal({
  entry,
  taken,
  onClose,
}: {
  entry: RegisterEntry | null;
  taken: string[];
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const me = useMe().data;
  const isNew = entry === null;
  const users = useQuery({
    queryKey: ["users"],
    queryFn: () => api.get<UserView[]>("/users"),
  });
  const modelsQ = useQuery({
    queryKey: ["registry", "model"],
    queryFn: () => api.get<{ alias: string }[]>("/registry/model"),
  });
  const [name, setName] = useState(entry?.name ?? "");
  const [purpose, setPurpose] = useState(entry?.purpose ?? "");
  const [owner, setOwner] = useState<string | null>(
    entry?.owner_id ?? me?.id ?? null,
  );
  const [costCentre, setCostCentre] = useState(entry?.cost_centre ?? "");
  const [tier, setTier] = useState(entry?.risk_tier ?? "TIER_3");
  const [autonomy, setAutonomy] = useState(entry?.autonomy ?? "A1");
  const [models, setModels] = useState<string[]>(entry?.allowed_models ?? []);
  const [highRisk, setHighRisk] = useState(entry?.eu_ai_act_high_risk ?? false);
  const [active, setActive] = useState(entry?.active ?? true);
  const [approval, setApproval] = useState("");
  const [needsApproval, setNeedsApproval] = useState(false);
  const [busy, setBusy] = useState(false);
  const owners = (users.data ?? [])
    .filter((u) => u.active)
    .map((u) => ({ value: u.id, label: `${u.name} (${u.email})` }));
  if (owner && !owners.some((o) => o.value === owner))
    owners.unshift({ value: owner, label: owner });
  const tierHelp = TIERS.find((t) => t.value === tier)?.help;
  return (
    <Modal
      opened
      onClose={onClose}
      title={isNew ? "Add an AI use" : `Edit ${entry.name}`}
      centered
      size="lg"
    >
      <Stack>
        {isNew && (
          <TextInput
            label="Name"
            placeholder="e.g. Coding assistant, Support chatbot"
            value={name}
            onChange={(x) => setName(x.currentTarget.value)}
            required
          />
        )}
        <Textarea
          label="What is it for?"
          placeholder="e.g. Developers use AI in VS Code to write and review code"
          value={purpose}
          onChange={(x) => setPurpose(x.currentTarget.value)}
          autosize
          minRows={2}
          required
        />
        <Group grow align="flex-start">
          <Select
            label="Owner"
            description="Who answers for this AI use"
            data={owners}
            value={owner}
            onChange={setOwner}
            searchable
          />
          <TextInput
            label="Cost centre"
            description="Where its AI spending is charged"
            placeholder="e.g. Engineering"
            value={costCentre}
            onChange={(x) => setCostCentre(x.currentTarget.value)}
          />
        </Group>
        <Select
          label="Risk level"
          description={tierHelp}
          data={TIERS.map(({ value, label }) => ({ value, label }))}
          value={tier}
          onChange={(v) => setTier(v ?? tier)}
          allowDeselect={false}
        />
        <Select
          label="What may the AI do?"
          data={AUTONOMY}
          value={autonomy}
          onChange={(v) => setAutonomy(v ?? autonomy)}
          allowDeselect={false}
        />
        <TagsInput
          label="Models it may use"
          description="Pick from Models & tools, or type a model name and press Enter. Requests for any other model are refused."
          data={(modelsQ.data ?? []).map((m) => m.alias).filter(Boolean)}
          value={models}
          onChange={setModels}
          placeholder="e.g. gpt-4o, claude-sonnet"
        />
        <Switch
          label="Counts as high-risk under the EU AI Act (needs Critical or High)"
          checked={highRisk}
          onChange={(x) => setHighRisk(x.currentTarget.checked)}
        />
        {!isNew && (
          <Switch
            label="Active (turn off to retire it; its keys stop working)"
            checked={active}
            onChange={(x) => setActive(x.currentTarget.checked)}
          />
        )}
        {needsApproval && (
          <>
            <Alert color="yellow">
              This change makes the checks weaker (lower risk level, more
              autonomy or high-risk removed). Enter the approval reference, e.g.
              a ticket number.
            </Alert>
            <TextInput
              label="Approval reference"
              value={approval}
              onChange={(x) => setApproval(x.currentTarget.value)}
              required
            />
          </>
        )}
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>
            Cancel
          </Button>
          <Button
            loading={busy}
            disabled={(isNew && !name.trim()) || !purpose.trim() || !owner}
            onClick={async () => {
              setBusy(true);
              const id = entry?.id ?? newId(name, taken);
              try {
                await api.put(`/register/${id}`, {
                  entry: {
                    ...(entry ?? {}),
                    id,
                    name: entry?.name ?? name.trim(),
                    purpose: purpose.trim(),
                    owner_id: owner,
                    cost_centre: costCentre.trim() || "general",
                    risk_tier: tier,
                    autonomy,
                    allowed_models: models,
                    allowed_tools: entry?.allowed_tools ?? [],
                    eu_ai_act_high_risk: highRisk,
                    active,
                  },
                  approval_ref: approval.trim() || null,
                });
                notifyOk(
                  isNew
                    ? "AI use added. You can now create Captain keys for it."
                    : "Saved",
                );
                await qc.invalidateQueries({ queryKey: ["register"] });
                onClose();
              } catch (err) {
                if (err instanceof ApiError && err.code === "APPROVAL_REQUIRED")
                  setNeedsApproval(true);
                else notifyError(err);
              } finally {
                setBusy(false);
              }
            }}
          >
            {isNew ? "Add" : "Save"}
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}
