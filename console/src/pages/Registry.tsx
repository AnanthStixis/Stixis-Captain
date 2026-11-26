import {
  Alert,
  Badge,
  Button,
  Checkbox,
  Code,
  Group,
  Modal,
  MultiSelect,
  NumberInput,
  ScrollArea,
  Select,
  Stack,
  Table,
  Tabs,
  Text,
  TextInput,
} from "@mantine/core";
import { IconPlus } from "@tabler/icons-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, type RegisterEntry } from "../api";
import { can, useMe } from "../App";
import {
  notifyError,
  notifyOk,
  PageHeader,
  QueryState,
} from "../components/common";

type Row = Record<string, unknown>;
interface Price {
  model: string;
  input_usd_per_m: number;
  output_usd_per_m: number;
}

const COLUMNS: Record<string, string[]> = {
  provider: ["id", "kind", "region", "base_url", "credential_ref", "active"],
  model: [
    "alias",
    "provider_id",
    "provider_model",
    "region",
    "capabilities",
    "active",
  ],
  route: ["alias", "targets", "max_retries"],
  server: ["id", "url", "transport", "owner_id", "active"],
  tool: ["name", "server_id", "impact", "risk", "read_only", "active"],
};

function cell(v: unknown) {
  if (typeof v === "boolean")
    return (
      <Badge color={v ? "teal" : "gray"} variant="light">
        {v ? "yes" : "no"}
      </Badge>
    );
  if (Array.isArray(v)) return v.join(" → ");
  if (v == null) return <Text c="dimmed">—</Text>;
  return <Code>{String(v)}</Code>;
}

const setPrice = (
  alias: string,
  input: number | string,
  output: number | string,
) =>
  api.put(`/prices/${encodeURIComponent(alias)}`, {
    input_usd_per_m: Number(input),
    output_usd_per_m: Number(output),
  });

export function RegistryPage() {
  const me = useMe().data;
  const write = can(me, "register:write");
  const [kind, setKind] = useState<string | null>("model");
  const [adding, setAdding] = useState(false);
  const [pricing, setPricing] = useState<string | null>(null);
  const q = useQuery({
    queryKey: ["registry", kind],
    queryFn: () => api.get<Row[]>(`/registry/${kind}`),
  });
  const prices = useQuery({
    queryKey: ["prices"],
    queryFn: () => api.get<Price[]>("/prices"),
  });
  const priceOf = (m: unknown) => prices.data?.find((p) => p.model === m);
  const cols = COLUMNS[kind ?? "model"] ?? [];
  return (
    <>
      <PageHeader
        title="Models & tools"
        description="The AI models your company may use, how Captain reaches them and what they cost. Add a model once; then allow it for your AI uses. Provider keys are entered on the AI keys page."
        actions={
          write && (
            <Button
              leftSection={<IconPlus size={16} />}
              onClick={() => setAdding(true)}
            >
              Add a model
            </Button>
          )
        }
      />
      <Tabs value={kind} onChange={setKind} mb="md">
        <Tabs.List>
          <Tabs.Tab value="model">Models</Tabs.Tab>
          <Tabs.Tab value="route">Routes</Tabs.Tab>
          <Tabs.Tab value="provider">Providers</Tabs.Tab>
          <Tabs.Tab value="server">MCP servers</Tabs.Tab>
          <Tabs.Tab value="tool">Tools</Tabs.Tab>
        </Tabs.List>
      </Tabs>
      <QueryState
        isLoading={q.isLoading}
        error={q.error}
        empty={q.data?.length === 0}
        emptyText={
          kind === "model"
            ? "No models yet. Press Add a model."
            : "Nothing registered."
        }
      >
        <ScrollArea>
          <Table striped miw={700}>
            <Table.Thead>
              <Table.Tr>
                {cols.map((c) => (
                  <Table.Th key={c}>{c.replaceAll("_", " ")}</Table.Th>
                ))}
                {kind === "model" && (
                  <Table.Th>Price (US$ per 1M tokens: in · out)</Table.Th>
                )}
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {q.data?.map((r, i) => {
                const p = priceOf(r.alias);
                return (
                  <Table.Tr key={i}>
                    {cols.map((c) => (
                      <Table.Td key={c}>{cell(r[c])}</Table.Td>
                    ))}
                    {kind === "model" && (
                      <Table.Td>
                        <Group gap="xs" wrap="nowrap">
                          {p ? (
                            <Text size="sm">
                              {p.input_usd_per_m} · {p.output_usd_per_m}
                            </Text>
                          ) : (
                            <Badge
                              color="red"
                              variant="light"
                              style={{ textTransform: "none" }}
                            >
                              No price: calls refused
                            </Badge>
                          )}
                          {write && (
                            <Button
                              size="xs"
                              variant="default"
                              onClick={() => setPricing(String(r.alias))}
                            >
                              Set price
                            </Button>
                          )}
                        </Group>
                      </Table.Td>
                    )}
                  </Table.Tr>
                );
              })}
            </Table.Tbody>
          </Table>
        </ScrollArea>
      </QueryState>
      {adding && <AddModel onClose={() => setAdding(false)} />}
      {pricing && (
        <SetPrice
          alias={pricing}
          current={priceOf(pricing)}
          onClose={() => setPricing(null)}
        />
      )}
    </>
  );
}

/** Dollars per million tokens, as on the provider's pricing page. */
function PriceFields({
  input,
  output,
  onInput,
  onOutput,
}: {
  input: number | string;
  output: number | string;
  onInput: (v: number | string) => void;
  onOutput: (v: number | string) => void;
}) {
  return (
    <Stack gap={4}>
      <Text size="sm" fw={500}>
        Price (US$ per million tokens)
      </Text>
      <Text size="xs" c="dimmed">
        From the provider's pricing page. Used for budgets and cost reports;
        calls are refused until a model has a price.
      </Text>
      <Group grow>
        <NumberInput
          label="Input (prompt)"
          placeholder="e.g. 0.15"
          min={0}
          decimalScale={4}
          value={input}
          onChange={onInput}
        />
        <NumberInput
          label="Output (answer)"
          placeholder="e.g. 0.60"
          min={0}
          decimalScale={4}
          value={output}
          onChange={onOutput}
        />
      </Group>
    </Stack>
  );
}

export function SetPrice({
  alias,
  current,
  onClose,
}: {
  alias: string;
  current?: Price;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [input, setInput] = useState<number | string>(
    current?.input_usd_per_m ?? "",
  );
  const [output, setOutput] = useState<number | string>(
    current?.output_usd_per_m ?? "",
  );
  const [busy, setBusy] = useState(false);
  return (
    <Modal opened onClose={onClose} title={`Price of ${alias}`} centered>
      <Stack>
        <PriceFields
          input={input}
          output={output}
          onInput={setInput}
          onOutput={setOutput}
        />
        <Text size="xs" c="dimmed">
          A new price applies from now on; earlier calls keep the price they
          were costed with.
        </Text>
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>
            Cancel
          </Button>
          <Button
            loading={busy}
            disabled={input === "" || output === ""}
            onClick={async () => {
              setBusy(true);
              try {
                await setPrice(alias, input, output);
                notifyOk("Price saved");
                await qc.invalidateQueries({ queryKey: ["prices"] });
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

const SUGGESTED: Record<string, string[]> = {
  openai: ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini", "o3-mini"],
  azure_openai: ["gpt-4o", "gpt-4o-mini"],
  anthropic: ["claude-sonnet-4-5", "claude-haiku-4-5", "claude-opus-4-1"],
};

/** One model = a model entry + a route of the same name + its price (+ allowed on the chosen AI uses). */
export function AddModel({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const providers = useQuery({
    queryKey: ["registry", "provider"],
    queryFn: () => api.get<Row[]>("/registry/provider"),
  });
  const register = useQuery({
    queryKey: ["register"],
    queryFn: () => api.get<RegisterEntry[]>("/register"),
  });
  const [provider, setProvider] = useState<string | null>(null);
  const [model, setModel] = useState("");
  const [alias, setAlias] = useState("");
  const [caps, setCaps] = useState<string[]>(["chat", "streaming", "tools"]);
  const [uses, setUses] = useState<string[] | null>(null);
  const [input, setInput] = useState<number | string>("");
  const [output, setOutput] = useState<number | string>("");
  const [busy, setBusy] = useState(false);
  const prov = providers.data?.find((p) => p.id === provider);
  const kindOf = String(prov?.kind ?? "");
  const name = (alias.trim() || model.trim()).toLowerCase();
  const activeUses = (register.data ?? []).filter((r) => r.active);
  const chosenUses = uses ?? activeUses.map((r) => r.id);
  return (
    <Modal opened onClose={onClose} title="Add a model" centered size="lg">
      <Stack>
        {providers.data && providers.data.length === 0 && (
          <Alert color="yellow">
            First add your provider key (OpenAI, Anthropic…) on the AI keys
            page.
          </Alert>
        )}
        <Select
          label="Provider"
          description="From the AI keys page"
          data={(providers.data ?? []).map((p) => ({
            value: String(p.id),
            label: `${String(p.id)} (${String(p.kind)})`,
          }))}
          value={provider}
          onChange={setProvider}
          allowDeselect={false}
        />
        <Group grow align="flex-start">
          <TextInput
            label="Model name at the provider"
            description={
              SUGGESTED[kindOf]
                ? `e.g. ${SUGGESTED[kindOf].join(", ")}`
                : "Exactly as the provider names it"
            }
            value={model}
            onChange={(e) => setModel(e.currentTarget.value)}
            required
          />
          <TextInput
            label="Name people use in Captain"
            description="Optional; defaults to the model name"
            placeholder={model || "e.g. gpt-4o"}
            value={alias}
            onChange={(e) => setAlias(e.currentTarget.value)}
          />
        </Group>
        <Checkbox.Group label="It can" value={caps} onChange={setCaps}>
          <Group mt={6}>
            <Checkbox value="chat" label="Chat" />
            <Checkbox value="streaming" label="Stream answers" />
            <Checkbox value="tools" label="Use tools" />
            <Checkbox value="vision" label="Read images" />
          </Group>
        </Checkbox.Group>
        <PriceFields
          input={input}
          output={output}
          onInput={setInput}
          onOutput={setOutput}
        />
        <MultiSelect
          label="Allow it for these AI uses"
          description="Keys of these AI uses may then use this model"
          data={activeUses.map((r) => ({ value: r.id, label: r.name }))}
          value={chosenUses}
          onChange={setUses}
        />
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>
            Cancel
          </Button>
          <Button
            loading={busy}
            disabled={
              !provider ||
              !model.trim() ||
              !caps.includes("chat") ||
              input === "" ||
              output === ""
            }
            onClick={async () => {
              setBusy(true);
              try {
                await api.put("/registry/model", {
                  alias: name,
                  provider_id: provider,
                  provider_model: model.trim(),
                  region: String(prov?.region ?? "external"),
                  capabilities: caps,
                });
                await api.put("/registry/route", {
                  alias: name,
                  targets: [name],
                });
                await setPrice(name, input, output);
                for (const r of activeUses.filter(
                  (x) =>
                    chosenUses.includes(x.id) &&
                    !x.allowed_models.includes(name),
                ))
                  await api.put(`/register/${r.id}`, {
                    entry: {
                      ...r,
                      allowed_models: [...r.allowed_models, name],
                    },
                    approval_ref: null,
                  });
                notifyOk(`Model ${name} added`);
                await qc.invalidateQueries({ queryKey: ["registry"] });
                await qc.invalidateQueries({ queryKey: ["register"] });
                await qc.invalidateQueries({ queryKey: ["prices"] });
                onClose();
              } catch (e) {
                notifyError(e);
              } finally {
                setBusy(false);
              }
            }}
          >
            Add model
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}
