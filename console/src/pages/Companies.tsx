import {
  Alert,
  Badge,
  Button,
  Code,
  CopyButton,
  Drawer,
  Group,
  Modal,
  Select,
  Stack,
  Table,
  Text,
  TextInput,
  Timeline,
  Title,
} from "@mantine/core";
import {
  IconBuilding,
  IconDownload,
  IconPlus,
  IconRefresh,
  IconRocket,
} from "@tabler/icons-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, download } from "../api";
import {
  notifyError,
  notifyOk,
  PageHeader,
  QueryState,
} from "../components/common";
import { when } from "../format";

interface Summary {
  company_name: string;
  adopters: string[];
  checklist_done: number;
  users: number;
  org_admins: number;
  active_keys: number;
  calls_last_7_days: number;
}

interface Company {
  code: string;
  details: {
    company_name: string;
    legal_name: string;
    domains: string[];
    country: string;
    industry: string;
  };
  status: "onboarding" | "active" | "suspended";
  region: string;
  environment: "pilot" | "prod";
  org_admin_email: string;
  org_admin_name: string;
  admin_url: string | null;
  created_at: string;
  bootstrapped_at: string | null;
  last_summary: Summary | null;
  last_checked: string | null;
  last_error: string | null;
  shared?: boolean; // one shared Captain: storage is created on Add, no install files
  provisioned?: boolean;
}

const STATUS_COLOR = {
  onboarding: "blue",
  active: "green",
  suspended: "gray",
} as const;

export function CompaniesPage() {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["companies"],
    queryFn: () => api.get<Company[]>("/companies"),
  });
  const [adding, setAdding] = useState(false);
  const [open, setOpen] = useState<string | null>(null);
  const refresh = () => qc.invalidateQueries({ queryKey: ["companies"] });
  return (
    <>
      <PageHeader
        title="Companies"
        description="Stixis only: the companies using Captain. Each company has its own separate Captain; this list holds its details, where its Captain is, and a health check. No company's AI activity or evidence comes here."
        actions={
          <Button
            leftSection={<IconPlus size={16} />}
            onClick={() => setAdding(true)}
          >
            Add a company
          </Button>
        }
      />
      <QueryState
        isLoading={q.isLoading}
        error={q.error}
        empty={q.data?.length === 0}
        emptyText="No companies yet. Add the first one."
      >
        <Table striped highlightOnHover>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Company</Table.Th>
              <Table.Th>Status</Table.Th>
              <Table.Th>Org admin</Table.Th>
              <Table.Th>Health</Table.Th>
              <Table.Th ta="right">Uses (7 days)</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {q.data?.map((c) => (
              <Table.Tr
                key={c.code}
                onClick={() => setOpen(c.code)}
                style={{ cursor: "pointer" }}
              >
                <Table.Td>
                  <Text fw={500}>{c.details.company_name}</Text>
                  <Text size="xs" c="dimmed">
                    {c.code} · {c.environment} · {c.region}
                  </Text>
                </Table.Td>
                <Table.Td>
                  <Badge color={STATUS_COLOR[c.status]} variant="light">
                    {c.status}
                  </Badge>
                </Table.Td>
                <Table.Td>{c.org_admin_email}</Table.Td>
                <Table.Td>
                  {c.shared && !c.provisioned ? (
                    <Badge color="orange" variant="light">
                      Storage not created
                    </Badge>
                  ) : !c.admin_url ? (
                    <Text size="sm" c="dimmed">
                      Not installed yet
                    </Text>
                  ) : c.last_error ? (
                    <Badge color="red" variant="light">
                      Problem
                    </Badge>
                  ) : c.last_summary ? (
                    <Badge color="green" variant="light">
                      OK {c.last_checked ? `· ${when(c.last_checked)}` : ""}
                    </Badge>
                  ) : (
                    <Text size="sm" c="dimmed">
                      Not checked yet
                    </Text>
                  )}
                </Table.Td>
                <Table.Td ta="right">
                  {c.last_summary
                    ? c.last_summary.calls_last_7_days.toLocaleString()
                    : ""}
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      </QueryState>
      {adding && (
        <AddCompany
          onClose={() => setAdding(false)}
          onAdded={async (code) => {
            setAdding(false);
            await refresh();
            setOpen(code);
          }}
        />
      )}
      <CompanyDrawer
        code={open}
        company={q.data?.find((c) => c.code === open) ?? null}
        onClose={() => setOpen(null)}
        onChanged={refresh}
      />
    </>
  );
}

function AddCompany({
  onClose,
  onAdded,
}: {
  onClose: () => void;
  onAdded: (code: string) => Promise<void>;
}) {
  const [f, setF] = useState({
    code: "",
    company_name: "",
    legal_name: "",
    domain: "",
    country: "",
    industry: "",
    region: "westeurope",
    environment: "pilot",
    email: "",
    name: "",
  });
  const [busy, setBusy] = useState(false);
  const set = (k: keyof typeof f) => (v: string) => setF({ ...f, [k]: v });
  const ready =
    /^[a-z][a-z0-9]{1,11}$/.test(f.code) &&
    f.company_name.trim() &&
    /@/.test(f.email);
  return (
    <Modal opened onClose={onClose} title="Add a company" centered size="lg">
      <Stack>
        <Group grow>
          <TextInput
            label="Company name"
            value={f.company_name}
            onChange={(e) => set("company_name")(e.currentTarget.value)}
            required
          />
          <TextInput
            label="Short code"
            description="2-12 lowercase letters/digits, used in install names"
            value={f.code}
            onChange={(e) => set("code")(e.currentTarget.value.toLowerCase())}
            required
          />
        </Group>
        <Group grow>
          <TextInput
            label="Legal name"
            value={f.legal_name}
            onChange={(e) => set("legal_name")(e.currentTarget.value)}
          />
          <TextInput
            label="Email domain"
            placeholder="abc.com"
            value={f.domain}
            onChange={(e) => set("domain")(e.currentTarget.value)}
          />
        </Group>
        <Group grow>
          <TextInput
            label="Country"
            value={f.country}
            onChange={(e) => set("country")(e.currentTarget.value)}
          />
          <TextInput
            label="Industry"
            value={f.industry}
            onChange={(e) => set("industry")(e.currentTarget.value)}
          />
        </Group>
        <Group grow>
          <TextInput
            label="Azure region"
            description="Where the company's data will live"
            value={f.region}
            onChange={(e) => set("region")(e.currentTarget.value)}
          />
          <Select
            label="Environment"
            data={["pilot", "prod"]}
            value={f.environment}
            onChange={(v) => set("environment")(v ?? "pilot")}
            allowDeselect={false}
          />
        </Group>
        <Title order={5}>Org admin</Title>
        <Group grow>
          <TextInput
            label="Email"
            value={f.email}
            onChange={(e) => set("email")(e.currentTarget.value)}
            required
          />
          <TextInput
            label="Name"
            value={f.name}
            onChange={(e) => set("name")(e.currentTarget.value)}
          />
        </Group>
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>
            Cancel
          </Button>
          <Button
            disabled={!ready}
            loading={busy}
            onClick={async () => {
              setBusy(true);
              try {
                await api.post("/companies", {
                  code: f.code,
                  details: {
                    company_name: f.company_name.trim(),
                    legal_name: f.legal_name.trim(),
                    domains: f.domain.trim()
                      ? [f.domain.trim().toLowerCase()]
                      : [],
                    country: f.country.trim(),
                    industry: f.industry.trim(),
                  },
                  region: f.region.trim(),
                  environment: f.environment,
                  org_admin_email: f.email.trim(),
                  org_admin_name: f.name.trim(),
                });
                notifyOk("Company added");
                await onAdded(f.code);
              } catch (e) {
                notifyError(e);
              } finally {
                setBusy(false);
              }
            }}
          >
            Add company
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}

function CompanyDrawer({
  code,
  company,
  onClose,
  onChanged,
}: {
  code: string | null;
  company: Company | null;
  onClose: () => void;
  onChanged: () => Promise<void>;
}) {
  const [url, setUrl] = useState("");
  const [invite, setInvite] = useState<{
    invite_url: string | null;
    message: string;
  } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const run = async (what: string, fn: () => Promise<void>) => {
    setBusy(what);
    try {
      await fn();
      await onChanged();
    } catch (e) {
      notifyError(e);
    } finally {
      setBusy(null);
    }
  };
  const c = company;
  const step = !c
    ? 0
    : c.last_summary
      ? 4
      : c.bootstrapped_at
        ? 3
        : c.admin_url
          ? 2
          : 1;
  const sharedStep = !c
    ? 0
    : c.last_summary
      ? 3
      : c.bootstrapped_at
        ? 2
        : c.provisioned
          ? 1
          : 0;
  return (
    <Drawer
      opened={!!code}
      onClose={() => {
        setInvite(null);
        onClose();
      }}
      title={c?.details.company_name}
      position="right"
      size="lg"
    >
      {c && (
        <Stack>
          <Text size="sm" c="dimmed">
            {c.code} · {c.environment} · {c.region} · added {when(c.created_at)}
          </Text>
          {c.shared ? (
            <Timeline active={sharedStep} bulletSize={22} lineWidth={2}>
              <Timeline.Item
                title="Company storage"
                bullet={<IconBuilding size={12} />}
              >
                {c.provisioned ? (
                  <Text size="sm" c="dimmed">
                    Created. The company's Captain is at{" "}
                    <Code>{c.admin_url}</Code>
                  </Text>
                ) : (
                  <>
                    {c.last_error && (
                      <Alert color="red" mt={4}>
                        {c.last_error}
                      </Alert>
                    )}
                    <Button
                      size="xs"
                      mt={6}
                      loading={busy === "prov"}
                      onClick={() =>
                        run("prov", async () => {
                          await api.post(`/companies/${c.code}/provision`);
                        })
                      }
                    >
                      Create it now
                    </Button>
                  </>
                )}
              </Timeline.Item>
              <Timeline.Item
                title="Create the org admin's sign-in link"
                bullet={<IconRocket size={12} />}
              >
                <Text size="sm" c="dimmed">
                  Creates {c.org_admin_email} as org admin of this company, with
                  a one-time sign-in link.
                </Text>
                <Button
                  size="xs"
                  mt={6}
                  disabled={!c.provisioned}
                  loading={busy === "boot"}
                  onClick={() =>
                    run("boot", async () =>
                      setInvite(
                        await api.post(`/companies/${c.code}/bootstrap`),
                      ),
                    )
                  }
                >
                  Bootstrap
                </Button>
                {invite && (
                  <Alert mt="xs" color={invite.invite_url ? "green" : "yellow"}>
                    {invite.invite_url ? (
                      <Stack gap={4}>
                        <Text size="sm">
                          Send this link to {c.org_admin_email}. It works once,
                          for 7 days, and is not kept here.
                        </Text>
                        <Code block style={{ wordBreak: "break-all" }}>
                          {invite.invite_url}
                        </Code>
                        <CopyButton value={invite.invite_url}>
                          {({ copied, copy }) => (
                            <Button size="xs" onClick={copy}>
                              {copied ? "Copied" : "Copy link"}
                            </Button>
                          )}
                        </CopyButton>
                      </Stack>
                    ) : (
                      invite.message
                    )}
                  </Alert>
                )}
              </Timeline.Item>
              <Timeline.Item
                title="Health check"
                bullet={<IconRefresh size={12} />}
              >
                {c.last_error && c.provisioned && (
                  <Alert color="red" mt={4}>
                    {c.last_error}
                  </Alert>
                )}
                {c.last_summary && (
                  <Text size="sm">
                    {c.last_summary.users} users ({c.last_summary.org_admins}{" "}
                    org admins) · {c.last_summary.active_keys} Captain keys ·{" "}
                    {c.last_summary.calls_last_7_days.toLocaleString()} uses in
                    7 days
                  </Text>
                )}
                <Button
                  size="xs"
                  mt={6}
                  variant="default"
                  disabled={!c.provisioned}
                  loading={busy === "check"}
                  onClick={() =>
                    run("check", async () => {
                      await api.post(`/companies/${c.code}/refresh`);
                    })
                  }
                >
                  Check now
                </Button>
              </Timeline.Item>
            </Timeline>
          ) : (
            <Timeline active={step} bulletSize={22} lineWidth={2}>
              <Timeline.Item
                title="Download the install files"
                bullet={<IconDownload size={12} />}
              >
                <Text size="sm" c="dimmed">
                  OpenTofu settings for this company, Helm values and its
                  platform token. Follow deploy/opentofu/azure/README.md, then
                  put the token in the install's Key Vault and delete the file.
                </Text>
                <Button
                  size="xs"
                  mt={6}
                  variant="default"
                  onClick={() =>
                    download(
                      `/companies/${c.code}/install-files`,
                      `captain-install-${c.code}.zip`,
                    ).catch(notifyError)
                  }
                >
                  Download install files
                </Button>
              </Timeline.Item>
              <Timeline.Item
                title="Where the company's Captain is"
                bullet={<IconBuilding size={12} />}
              >
                {c.admin_url && <Code>{c.admin_url}</Code>}
                <Group mt={6} gap="xs" wrap="nowrap">
                  <TextInput
                    size="xs"
                    placeholder="https://captain-admin.abc.com"
                    value={url}
                    onChange={(e) => setUrl(e.currentTarget.value)}
                    style={{ flex: 1 }}
                  />
                  <Button
                    size="xs"
                    variant="default"
                    disabled={!url.trim()}
                    loading={busy === "url"}
                    onClick={() =>
                      run("url", async () => {
                        await api.put(`/companies/${c.code}/install`, {
                          admin_url: url.trim(),
                        });
                        setUrl("");
                        notifyOk("Address saved");
                      })
                    }
                  >
                    Save
                  </Button>
                </Group>
              </Timeline.Item>
              <Timeline.Item
                title="Create the org admin's sign-in link"
                bullet={<IconRocket size={12} />}
              >
                <Text size="sm" c="dimmed">
                  Sends the company details to its Captain and creates{" "}
                  {c.org_admin_email} as org admin, with a one-time sign-in
                  link.
                </Text>
                <Button
                  size="xs"
                  mt={6}
                  disabled={!c.admin_url}
                  loading={busy === "boot"}
                  onClick={() =>
                    run("boot", async () =>
                      setInvite(
                        await api.post(`/companies/${c.code}/bootstrap`),
                      ),
                    )
                  }
                >
                  Bootstrap
                </Button>
                {invite && (
                  <Alert mt="xs" color={invite.invite_url ? "green" : "yellow"}>
                    {invite.invite_url ? (
                      <Stack gap={4}>
                        <Text size="sm">
                          Send this link to {c.org_admin_email}. It works once,
                          for 7 days, and is not kept here.
                        </Text>
                        <Code block style={{ wordBreak: "break-all" }}>
                          {invite.invite_url}
                        </Code>
                        <CopyButton value={invite.invite_url}>
                          {({ copied, copy }) => (
                            <Button size="xs" onClick={copy}>
                              {copied ? "Copied" : "Copy link"}
                            </Button>
                          )}
                        </CopyButton>
                      </Stack>
                    ) : (
                      invite.message
                    )}
                  </Alert>
                )}
              </Timeline.Item>
              <Timeline.Item
                title="Health check"
                bullet={<IconRefresh size={12} />}
              >
                {c.last_error && (
                  <Alert color="red" mt={4}>
                    {c.last_error}
                  </Alert>
                )}
                {c.last_summary && (
                  <Text size="sm">
                    {c.last_summary.users} users ({c.last_summary.org_admins}{" "}
                    org admins) · {c.last_summary.active_keys} Captain keys ·{" "}
                    {c.last_summary.calls_last_7_days.toLocaleString()} uses in
                    7 days · tools:{" "}
                    {c.last_summary.adopters.join(", ") || "none chosen"}
                  </Text>
                )}
                <Button
                  size="xs"
                  mt={6}
                  variant="default"
                  disabled={!c.admin_url}
                  loading={busy === "check"}
                  onClick={() =>
                    run("check", async () => {
                      await api.post(`/companies/${c.code}/refresh`);
                    })
                  }
                >
                  Check now
                </Button>
              </Timeline.Item>
            </Timeline>
          )}
          <Group>
            <Text size="sm">Status:</Text>
            {(["onboarding", "active", "suspended"] as const).map((s) => (
              <Button
                key={s}
                size="xs"
                variant={c.status === s ? "filled" : "default"}
                color={STATUS_COLOR[s]}
                onClick={() =>
                  run("status", async () => {
                    await api.put(`/companies/${c.code}`, { status: s });
                  })
                }
              >
                {s}
              </Button>
            ))}
          </Group>
        </Stack>
      )}
    </Drawer>
  );
}
