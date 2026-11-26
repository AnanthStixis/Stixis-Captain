import { Alert, Badge, Button, Card, Checkbox, Code, CopyButton, Group, List, Loader, Modal, SimpleGrid, Stack, Table, Text, Title } from "@mantine/core";
import { IconBook, IconDownload } from "@tabler/icons-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useState, type ReactNode } from "react";

import { api, download, getText, type Organization } from "../api";
import { can, useMe } from "../App";
import { notifyError, notifyOk, PageHeader, QueryState } from "../components/common";

interface DownloadItem {
  id: string;
  label: string;
  filename: string;
  note: string;
}

interface AdopterItem {
  id: string;
  name: string;
  group: string;
  governs: string;
  channel: string;
  guide: string;
  calls_last_7_days: number;
  channel_shared: boolean;
  downloads: DownloadItem[];
}

interface AdoptersResponse {
  urls: { gateway: string; mcp: string; core: string; console: string };
  items: AdopterItem[];
}

export function ConnectToolsPage() {
  const me = useMe().data;
  const qc = useQueryClient();
  const write = can(me, "org:write");
  const adopters = useQuery({ queryKey: ["adopters"], queryFn: () => api.get<AdoptersResponse>("/adopters") });
  const org = useQuery({ queryKey: ["organization"], queryFn: () => api.get<Organization>("/organization") });
  const [guide, setGuide] = useState<AdopterItem | null>(null);
  const chosen = new Set(org.data?.adopters ?? []);

  const toggle = async (id: string, on: boolean) => {
    const o = org.data;
    if (!o) return;
    const next = on ? [...o.adopters.filter((a) => a !== id), id] : o.adopters.filter((a) => a !== id);
    try {
      await api.put("/organization", {
        company_name: o.company_name,
        legal_name: o.legal_name,
        domains: o.domains,
        country: o.country,
        industry: o.industry,
        contacts: o.contacts,
        adopters: next,
        checklist: { ...o.checklist, adopters: next.length > 0 },
      });
      await qc.invalidateQueries({ queryKey: ["organization"] });
      notifyOk(on ? "Added to your tools" : "Removed from your tools");
    } catch (e) {
      notifyError(e);
    }
  };

  const groups = [...new Set(adopters.data?.items.map((a) => a.group) ?? [])];
  return (
    <>
      <PageHeader
        title="Connect your tools"
        description="Choose how your people and systems will use AI through Captain. Each tool has downloads already set up for your company, a step-by-step install guide, and a short test to check it works."
      />
      <QueryState isLoading={adopters.isLoading || org.isLoading} error={adopters.error ?? org.error}>
        {adopters.data && (
          <Alert variant="light" mb="lg" title="Your Captain addresses">
            <AddressRow label="AI gateway" value={adopters.data.urls.gateway} />
            <AddressRow label="MCP tools" value={adopters.data.urls.mcp} />
            <Text size="xs" c="dimmed" mt={4}>
              Downloads never contain a key. Each person or system uses its own Captain key (Captain keys page).
            </Text>
          </Alert>
        )}
        <Stack gap="xl">
          {groups.map((g) => (
            <div key={g}>
              <Title order={4} mb="sm">
                {g}
              </Title>
              <SimpleGrid cols={{ base: 1, md: 2 }}>
                {adopters.data?.items
                  .filter((a) => a.group === g)
                  .map((a) => (
                    <Card key={a.id} withBorder radius="md" padding="lg">
                      <Group justify="space-between" align="flex-start" wrap="nowrap">
                        <Checkbox
                          label={<Text fw={600}>{a.name}</Text>}
                          checked={chosen.has(a.id)}
                          disabled={!write}
                          onChange={(e) => void toggle(a.id, e.currentTarget.checked)}
                        />
                        {a.calls_last_7_days > 0 ? (
                          <Badge color="green" variant="light" miw="max-content" title={a.channel_shared ? `Counts every ${a.channel} call: this channel is shared by several tools.` : undefined}>
                            {a.channel_shared ? `${a.calls_last_7_days.toLocaleString()} ${a.channel} uses in 7 days` : `${a.calls_last_7_days.toLocaleString()} uses in 7 days`}
                          </Badge>
                        ) : (
                          <Badge color="gray" variant="light" miw="max-content">
                            No use yet
                          </Badge>
                        )}
                      </Group>
                      <Text size="sm" mt="xs">
                        {a.governs}
                      </Text>
                      <Stack gap={6} mt="md">
                        {a.downloads.map((d) => (
                          <Group key={d.id} justify="space-between" wrap="nowrap">
                            <div>
                              <Text size="sm">{d.label}</Text>
                              {d.note && (
                                <Text size="xs" c="dimmed">
                                  {d.note}
                                </Text>
                              )}
                            </div>
                            <Button size="xs" variant="default" style={{ flexShrink: 0 }} leftSection={<IconDownload size={14} />} onClick={() => download(`/downloads/${d.id}`, d.filename).catch(notifyError)}>
                              Download
                            </Button>
                          </Group>
                        ))}
                      </Stack>
                      <Button mt="md" variant="light" leftSection={<IconBook size={16} />} onClick={() => setGuide(a)}>
                        Install and test guide
                      </Button>
                    </Card>
                  ))}
              </SimpleGrid>
            </div>
          ))}
        </Stack>
      </QueryState>
      <GuideModal item={guide} onClose={() => setGuide(null)} />
    </>
  );
}

function AddressRow({ label, value }: { label: string; value: string }) {
  return (
    <Group gap="xs">
      <Text size="sm" w={110}>
        {label}
      </Text>
      <Code>{value}</Code>
      <CopyButton value={value}>
        {({ copied, copy }) => (
          <Button size="compact-xs" variant="subtle" onClick={copy}>
            {copied ? "Copied" : "Copy"}
          </Button>
        )}
      </CopyButton>
    </Group>
  );
}

function GuideModal({ item, onClose }: { item: AdopterItem | null; onClose: () => void }) {
  const q = useQuery({ queryKey: ["guide", item?.guide], queryFn: () => getText(`/guides/${item!.guide}`), enabled: !!item });
  return (
    <Modal opened={!!item} onClose={onClose} title={item?.name} size="xl">
      {q.isLoading ? <Loader /> : q.error ? <Alert color="red">This guide could not be loaded.</Alert> : <Markdown text={q.data ?? ""} />}
    </Modal>
  );
}

// A small renderer for the guides (headings, lists, tables, code blocks, bold, code, links).
function inline(text: string): ReactNode[] {
  const parts: ReactNode[] = [];
  const re = /(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\))/g;
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text))) {
    if (m.index > last) parts.push(text.slice(last, m.index));
    const t = m[0];
    if (t.startsWith("**")) parts.push(<b key={m.index}>{t.slice(2, -2)}</b>);
    else if (t.startsWith("`")) parts.push(<Code key={m.index}>{t.slice(1, -1)}</Code>);
    else parts.push(<Fragment key={m.index}>{t.slice(1, t.indexOf("]"))}</Fragment>);
    last = m.index + t.length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return parts;
}

export function Markdown({ text }: { text: string }) {
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  const at = (k: number): string => lines[k] ?? "";
  const isItem = (l: string) => /^\s*(\d+\.|-) /.test(l);
  const isFence = (l: string) => l.trim().startsWith("```");
  const out: ReactNode[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = at(i);
    if (isFence(line)) {
      const body: string[] = [];
      i++;
      while (i < lines.length && !isFence(at(i))) body.push(at(i++).replace(/^ {3}/, ""));
      i++;
      const code = body.join("\n");
      out.push(
        <div key={i} style={{ position: "relative" }}>
          <Code block>{code}</Code>
          <CopyButton value={code}>
            {({ copied, copy }) => (
              <Button size="compact-xs" variant="default" onClick={copy} style={{ position: "absolute", top: 6, right: 6 }}>
                {copied ? "Copied" : "Copy"}
              </Button>
            )}
          </CopyButton>
        </div>,
      );
      continue;
    }
    if (/^#{1,3} /.test(line)) {
      const level = line.indexOf(" ");
      out.push(
        <Title key={i} order={(level + 1) as 2 | 3 | 4} mt={level === 1 ? 0 : "md"}>
          {inline(line.slice(level + 1))}
        </Title>,
      );
      i++;
      continue;
    }
    if (line.startsWith("|")) {
      const rows: string[][] = [];
      while (i < lines.length && at(i).startsWith("|")) {
        const cells = at(i)
          .split("|")
          .slice(1, -1)
          .map((c) => c.trim());
        if (!cells.every((c) => /^-+$/.test(c))) rows.push(cells);
        i++;
      }
      const [head = [], ...body] = rows;
      out.push(
        <Table key={i} withTableBorder striped>
          <Table.Thead>
            <Table.Tr>
              {head.map((c, k) => (
                <Table.Th key={k}>{inline(c)}</Table.Th>
              ))}
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {body.map((r, k) => (
              <Table.Tr key={k}>
                {r.map((c, j) => (
                  <Table.Td key={j}>{inline(c)}</Table.Td>
                ))}
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>,
      );
      continue;
    }
    if (isItem(line)) {
      const ordered = /^\s*\d+\./.test(line);
      const items: ReactNode[] = [];
      while (i < lines.length && isItem(at(i))) {
        const raw = at(i).replace(/^\s*(\d+\.|-) /, "");
        const sub: string[] = [];
        i++;
        // Indented lines belong to this step; an indented code block ends it (rendered after).
        while (i < lines.length && /^\s{2,}\S/.test(at(i)) && !isFence(at(i))) sub.push(at(i++).trim().replace(/^- /, "• "));
        items.push(
          <List.Item key={items.length}>
            {inline(raw)}
            {sub.map((t, k) => (
              <Text key={k} size="sm">
                {inline(t)}
              </Text>
            ))}
          </List.Item>,
        );
      }
      out.push(
        <List key={i} type={ordered ? "ordered" : "unordered"} spacing={4} withPadding>
          {items}
        </List>,
      );
      continue;
    }
    if (line.trim()) out.push(<Text key={i}>{inline(line)}</Text>);
    i++;
  }
  return <Stack gap="sm">{out}</Stack>;
}
