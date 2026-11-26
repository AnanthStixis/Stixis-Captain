import { Badge, Code, ScrollArea, Table, Tabs, Text } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "../api";
import { PageHeader, QueryState } from "../components/common";

type Row = Record<string, unknown>;

const COLUMNS: Record<string, string[]> = {
  provider: ["id", "kind", "region", "base_url", "credential_ref", "active"],
  model: ["alias", "provider_id", "provider_model", "region", "capabilities", "active"],
  route: ["alias", "targets", "max_retries"],
  server: ["id", "url", "transport", "owner_id", "active"],
  tool: ["name", "server_id", "impact", "risk", "read_only", "active"],
};

function cell(v: unknown) {
  if (typeof v === "boolean") return <Badge color={v ? "teal" : "gray"} variant="light">{v ? "yes" : "no"}</Badge>;
  if (Array.isArray(v)) return v.join(" → ");
  if (v == null) return <Text c="dimmed">—</Text>;
  return <Code>{String(v)}</Code>;
}

export function RegistryPage() {
  const [kind, setKind] = useState<string | null>("model");
  const q = useQuery({ queryKey: ["registry", kind], queryFn: () => api.get<Row[]>(`/registry/${kind}`) });
  const cols = COLUMNS[kind ?? "model"] ?? [];
  return (
    <>
      <PageHeader
        title="Models & tools"
        description="What exists and how to reach it: providers, model aliases, fallback routes, MCP servers and tools. Credentials are referenced by name only; Captain never stores provider keys."
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
      <QueryState isLoading={q.isLoading} error={q.error} empty={q.data?.length === 0} emptyText="Nothing registered.">
        <ScrollArea>
          <Table striped miw={700}>
            <Table.Thead>
              <Table.Tr>
                {cols.map((c) => (
                  <Table.Th key={c}>{c.replaceAll("_", " ")}</Table.Th>
                ))}
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {q.data?.map((r, i) => (
                <Table.Tr key={i}>
                  {cols.map((c) => (
                    <Table.Td key={c}>{cell(r[c])}</Table.Td>
                  ))}
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </ScrollArea>
      </QueryState>
    </>
  );
}
