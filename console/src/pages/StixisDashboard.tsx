import { Badge, Card, Group, Progress, SimpleGrid, Stack, Text, Title } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";

import { api } from "../api";
import { PageHeader, QueryState, StatCard } from "../components/common";
import { when } from "../format";

interface Dashboard {
  companies: number;
  status: { onboarding: number; active: number; suspended: number };
  health: { healthy: number; problem: number; not_installed: number; not_checked: number };
  pipeline: { added: number; address_set: number; bootstrapped: number; healthy: number };
  totals: { users: number; org_admins: number; active_keys: number; calls_last_7_days: number };
  needs_attention: { code: string; name: string; reason: string }[];
  top_usage: { code: string; name: string; calls_last_7_days: number }[];
  recent_changes: { when: string; headline: string }[];
}

const STEPS: [keyof Dashboard["pipeline"], string][] = [
  ["added", "Added"],
  ["address_set", "Install connected"],
  ["bootstrapped", "Org admin invited"],
  ["healthy", "Healthy"],
];

export function StixisDashboardPage() {
  const nav = useNavigate();
  const q = useQuery({ queryKey: ["stixis-dashboard"], queryFn: () => api.get<Dashboard>("/companies/dashboard"), refetchInterval: 60_000 });
  const d = q.data;
  const maxUse = Math.max(1, ...(d?.top_usage.map((u) => u.calls_last_7_days) ?? [1]));
  return (
    <>
      <PageHeader title="Dashboard" description="All companies on Captain at a glance. Built from each company's health check (counts only); no company's AI activity or evidence comes here." />
      <QueryState isLoading={q.isLoading} error={q.error}>
        {d && (
          <Stack>
            <SimpleGrid cols={{ base: 2, md: 4 }}>
              <StatCard label="Companies" value={d.companies} hint={`${d.status.active} active · ${d.status.onboarding} onboarding · ${d.status.suspended} suspended`} />
              <StatCard label="Healthy installs" value={d.health.healthy} color="green" hint={`${d.health.problem} with a problem`} />
              <StatCard label="People on Captain" value={d.totals.users.toLocaleString()} hint={`${d.totals.org_admins} org admins`} />
              <StatCard label="AI uses (7 days)" value={d.totals.calls_last_7_days.toLocaleString()} hint={`${d.totals.active_keys} Captain keys in use`} />
            </SimpleGrid>
            <SimpleGrid cols={{ base: 1, md: 2 }}>
              <Card withBorder>
                <Title order={5} mb="sm">Onboarding</Title>
                <Stack gap="xs">
                  {STEPS.map(([k, label]) => (
                    <div key={k}>
                      <Group justify="space-between">
                        <Text size="sm">{label}</Text>
                        <Text size="sm" fw={500}>{d.pipeline[k]} / {d.companies}</Text>
                      </Group>
                      <Progress value={d.companies ? (100 * d.pipeline[k]) / d.companies : 0} color={k === "healthy" ? "green" : "blue"} />
                    </div>
                  ))}
                </Stack>
              </Card>
              <Card withBorder>
                <Title order={5} mb="sm">Needs attention</Title>
                {d.needs_attention.length === 0 ? (
                  <Text size="sm" c="dimmed">Nothing right now. All companies are fine.</Text>
                ) : (
                  <Stack gap="xs">
                    {d.needs_attention.map((a) => (
                      <Group key={a.code + a.reason} justify="space-between" wrap="nowrap" style={{ cursor: "pointer" }} onClick={() => nav("/companies")}>
                        <Text size="sm" fw={500}>{a.name}</Text>
                        <Badge color="orange" variant="light" style={{ textTransform: "none" }}>{a.reason}</Badge>
                      </Group>
                    ))}
                  </Stack>
                )}
              </Card>
              <Card withBorder>
                <Title order={5} mb="sm">Most AI use (7 days)</Title>
                {d.top_usage.length === 0 ? (
                  <Text size="sm" c="dimmed">No health checks with usage yet.</Text>
                ) : (
                  <Stack gap="xs">
                    {d.top_usage.map((u) => (
                      <div key={u.code}>
                        <Group justify="space-between">
                          <Text size="sm">{u.name}</Text>
                          <Text size="sm" fw={500}>{u.calls_last_7_days.toLocaleString()}</Text>
                        </Group>
                        <Progress value={(100 * u.calls_last_7_days) / maxUse} />
                      </div>
                    ))}
                  </Stack>
                )}
              </Card>
              <Card withBorder>
                <Title order={5} mb="sm">Recent changes</Title>
                {d.recent_changes.length === 0 ? (
                  <Text size="sm" c="dimmed">No company changes yet.</Text>
                ) : (
                  <Stack gap={6}>
                    {d.recent_changes.map((r, i) => (
                      <Group key={i} justify="space-between" wrap="nowrap" align="flex-start">
                        <Text size="sm">{r.headline}</Text>
                        <Text size="xs" c="dimmed" style={{ whiteSpace: "nowrap" }}>{when(r.when)}</Text>
                      </Group>
                    ))}
                  </Stack>
                )}
              </Card>
            </SimpleGrid>
          </Stack>
        )}
      </QueryState>
    </>
  );
}
