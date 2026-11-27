import {
  Alert,
  AppShell,
  Badge,
  Burger,
  Button,
  Center,
  Divider,
  Group,
  Loader,
  NavLink,
  Paper,
  PasswordInput,
  Stack,
  Text,
  TextInput,
  Title,
} from "@mantine/core";
import { useDisclosure } from "@mantine/hooks";
import {
  IconActivity,
  IconAlertTriangle,
  IconBook2,
  IconRobot,
  IconChartPie,
  IconBox,
  IconCash,
  IconChecklist,
  IconEye,
  IconUsers,
  IconUserCog,
  IconListCheck,
  IconGauge,
  IconKey,
  IconLock,
  IconBuildingSkyscraper,
  IconPlugConnected,
  IconLogout,
  IconPower,
  IconScale,
  IconShieldCheck,
  type Icon,
} from "@tabler/icons-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import {
  Navigate,
  Route,
  Routes,
  useLocation,
  useNavigate,
} from "react-router-dom";

import {
  api,
  ApiError,
  getToken,
  redeemInvite,
  setCsrf,
  setToken,
  type AuthConfig,
  type Me,
  type SessionInfo,
} from "./api";
import { notifyError } from "./components/common";
import { AlertsPage } from "./pages/Alerts";
import { ApprovalsPage } from "./pages/Approvals";
import { AuditPage } from "./pages/Audit";
import { BudgetsPage } from "./pages/Budgets";
import { EventsPage } from "./pages/Events";
import { AiKeysPage } from "./pages/AiKeys";
import { CompaniesPage } from "./pages/Companies";
import { StixisDashboardPage } from "./pages/StixisDashboard";
import { ConnectToolsPage } from "./pages/ConnectTools";
import { KeysPage } from "./pages/Keys";
import { KillSwitchPage } from "./pages/KillSwitch";
import { AgentsPage } from "./pages/Agents";
import { CoveragePage } from "./pages/Coverage";
import { ObservedPage } from "./pages/Observed";
import { PolicyPage } from "./pages/Policy";
import { OverviewPage } from "./pages/Overview";
import { RegisterPage } from "./pages/Register";
import { RegistryPage } from "./pages/Registry";
import { SetupPage } from "./pages/Setup";
import { UsageBreakdownPage } from "./pages/UsageBreakdown";
import { UsersPage } from "./pages/Users";

interface NavItem {
  path: string;
  label: string;
  icon: Icon;
  needs: string; // permission required to see the page
  element: JSX.Element;
}

export const NAV: NavItem[] = [
  {
    path: "/",
    label: "Overview",
    icon: IconGauge,
    needs: "usage:read",
    element: <OverviewPage />,
  },
  {
    path: "/activity",
    label: "Activity",
    icon: IconActivity,
    needs: "usage:read",
    element: <EventsPage />,
  },
  {
    path: "/usage",
    label: "Usage by…",
    icon: IconUsers,
    needs: "usage:read",
    element: <UsageBreakdownPage />,
  },
  {
    path: "/coverage",
    label: "Coverage",
    icon: IconChartPie,
    needs: "usage:read",
    element: <CoveragePage />,
  },
  {
    path: "/agents",
    label: "Agents & systems",
    icon: IconRobot,
    needs: "register:read",
    element: <AgentsPage />,
  },
  {
    path: "/observed",
    label: "Observed usage",
    icon: IconEye,
    needs: "usage:read",
    element: <ObservedPage />,
  },
  {
    path: "/approvals",
    label: "Approvals",
    icon: IconChecklist,
    needs: "approval:decide",
    element: <ApprovalsPage />,
  },
  {
    path: "/budgets",
    label: "Budgets",
    icon: IconCash,
    needs: "budget:read",
    element: <BudgetsPage />,
  },
  {
    path: "/keys",
    label: "Captain keys",
    icon: IconKey,
    needs: "keys:read",
    element: <KeysPage />,
  },
  {
    path: "/ai-keys",
    label: "AI keys",
    icon: IconLock,
    needs: "register:read",
    element: <AiKeysPage />,
  },
  {
    path: "/connect",
    label: "Connect your tools",
    icon: IconPlugConnected,
    needs: "register:read",
    element: <ConnectToolsPage />,
  },
  {
    path: "/stixis",
    label: "Dashboard (Stixis)",
    icon: IconBuildingSkyscraper,
    needs: "companies:write",
    element: <StixisDashboardPage />,
  },
  {
    path: "/companies",
    label: "Companies (Stixis)",
    icon: IconBuildingSkyscraper,
    needs: "companies:write",
    element: <CompaniesPage />,
  },
  {
    path: "/register",
    label: "AI Register",
    icon: IconBook2,
    needs: "register:read",
    element: <RegisterPage />,
  },
  {
    path: "/registry",
    label: "Models & tools",
    icon: IconBox,
    needs: "register:read",
    element: <RegistryPage />,
  },
  {
    path: "/policy",
    label: "Policy",
    icon: IconScale,
    needs: "usage:read",
    element: <PolicyPage />,
  },
  {
    path: "/killswitch",
    label: "Kill switch",
    icon: IconPower,
    needs: "usage:read",
    element: <KillSwitchPage />,
  },
  {
    path: "/alerts",
    label: "Alerts",
    icon: IconAlertTriangle,
    needs: "usage:read",
    element: <AlertsPage />,
  },
  {
    path: "/audit",
    label: "Audit & reports",
    icon: IconShieldCheck,
    needs: "audit:read",
    element: <AuditPage />,
  },
  {
    path: "/users",
    label: "Users",
    icon: IconUserCog,
    needs: "keys:read",
    element: <UsersPage />,
  },
  {
    path: "/setup",
    label: "Setup",
    icon: IconListCheck,
    needs: "org:write",
    element: <SetupPage />,
  },
];

/** /accept-invite#token=…: a one-time org-admin invite. The token travels in the URL
 * fragment, so it never reaches server logs; it is posted once and removed from the URL. */
function AcceptInvite({ onDone }: { onDone: () => void }) {
  const [error, setError] = useState<string | null>(null);
  const started = useRef(false);
  const nav = useNavigate();
  useEffect(() => {
    if (started.current) return;
    started.current = true;
    const token =
      new URLSearchParams(window.location.hash.slice(1)).get("token") ?? "";
    window.history.replaceState({}, "", "/accept-invite");
    redeemInvite(token)
      .then((s) => {
        // An org admin's first sign-in lands on the setup checklist; everyone else on home.
        nav(s.permissions.includes("org:write") ? "/setup" : "/", {
          replace: true,
        });
        onDone();
      })
      .catch((e: unknown) =>
        setError(
          e instanceof ApiError
            ? e.message
            : "This sign-in link could not be used.",
        ),
      );
  }, [nav, onDone]);
  return (
    <Center mih="100vh">
      {error ? (
        <Paper withBorder p="xl" w={460} maw="94vw">
          <Stack>
            <Title order={3}>Sign-in link not accepted</Title>
            <Alert
              color="red"
              icon={<IconAlertTriangle size={16} />}
              role="alert"
            >
              {error}
            </Alert>
            <Button component="a" href="/" variant="default">
              Go to sign-in
            </Button>
          </Stack>
        </Paper>
      ) : (
        <Loader aria-label="Signing you in" />
      )}
    </Center>
  );
}

const STIXIS_MENU = [
  { path: "/stixis", label: "Dashboard" },
  { path: "/companies", label: "Companies" },
  { path: "/users", label: "Stixis staff" },
  { path: "/audit", label: "Audit" },
];

const ROLE_LABELS: Record<string, string> = {
  "platform-admin": "Platform admin",
  "org-admin": "Org admin",
  admin: "Admin",
  approver: "Approver",
  auditor: "Auditor",
  finops: "Finance",
  viewer: "Viewer",
  content_reviewer: "Content reviewer",
};

/** Roles in plain words. On the Stixis console only the platform role matters. */
export function roleText(roles: string[], stixis: boolean): string {
  if (stixis && roles.includes("platform-admin")) return "Platform admin";
  return roles.map((r) => ROLE_LABELS[r] ?? r).join(", ");
}

/** True on Stixis's control plane (CAPTAIN_CONTROL_PLANE=1), from the public auth config. */
export function useStixis(): boolean {
  const q = useQuery({
    queryKey: ["auth-config"],
    queryFn: () => api.get<AuthConfig>("/auth/config"),
    staleTime: Infinity,
  });
  return !!q.data?.stixis_console;
}

export function useMe() {
  return useQuery({
    queryKey: ["me"],
    queryFn: async (): Promise<Me> => {
      if (getToken()) return api.get<Me>("/me"); // dev token
      const s = await api.get<SessionInfo>("/auth/session"); // SSO session cookie
      setCsrf(s.csrf_token);
      return s;
    },
    retry: false,
  });
}

export function can(me: Me | undefined, permission: string): boolean {
  return !!me?.permissions.includes(permission);
}

const SSO_ERRORS: Record<string, string> = {
  no_role:
    "You signed in, but no Captain role is assigned to your groups. Ask your administrator for access.",
  denied: "Sign-in was cancelled or denied at your identity provider.",
  state:
    "The sign-in session expired or could not be verified. Please try again.",
  groups_overage:
    "You belong to too many groups for the sign-in token. Ask your administrator to send Captain app roles, or only the Captain groups.",
  failed:
    "Single sign-on could not be completed. Please try again, or contact your administrator.",
};

/** Companies without SSO: a one-time sign-in link by email (15 minutes, single use). */
function EmailLinkForm() {
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState<string | null>(null);
  return sent ? (
    <Alert color="green" title="Check your email">
      {sent}
    </Alert>
  ) : (
    <form
      onSubmit={async (e) => {
        e.preventDefault();
        setBusy(true);
        try {
          const r = await api.post<{ status: string }>("/auth/email-link", {
            email: email.trim(),
          });
          setSent(r.status);
        } catch (err) {
          notifyError(err);
        } finally {
          setBusy(false);
        }
      }}
    >
      <Stack gap="xs">
        <TextInput
          label="Work email"
          type="email"
          placeholder="you@company.com"
          value={email}
          onChange={(e) => setEmail(e.currentTarget.value)}
          required
        />
        <Button
          type="submit"
          loading={busy}
          disabled={!/^[^@s]+@[^@s]+.[^@s]+$/.test(email.trim())}
        >
          Email me a sign-in link
        </Button>
      </Stack>
    </form>
  );
}

function Login({ onSignedIn }: { onSignedIn: () => void }) {
  const [token, setTok] = useState("");
  const [busy, setBusy] = useState(false);
  const cfg = useQuery({
    queryKey: ["auth-config"],
    queryFn: () => api.get<AuthConfig>("/auth/config"),
    retry: 1,
  });
  const [ssoError] = useState(() => {
    const code = new URLSearchParams(window.location.search).get("sso_error");
    if (code) window.history.replaceState({}, "", window.location.pathname); // keep the URL clean
    return code ? (SSO_ERRORS[code] ?? SSO_ERRORS.failed) : null;
  });
  return (
    <Center mih="100vh" bg="var(--mantine-color-gray-light)">
      <Paper withBorder shadow="sm" p="xl" w={420} maw="94vw">
        <Stack>
          <div>
            <Title order={2}>
              {cfg.data?.stixis_console ? "Stixis console" : "Captain Console"}
            </Title>
            <Text c="dimmed" size="sm">
              Central control for AI governance
            </Text>
          </div>
          {ssoError && (
            <Alert
              color="red"
              icon={<IconAlertTriangle size={16} />}
              role="alert"
            >
              {ssoError}
            </Alert>
          )}
          {cfg.isLoading ? (
            <Center>
              <Loader size="sm" aria-label="Loading" />
            </Center>
          ) : (
            <>
              {cfg.data?.email_link && <EmailLinkForm />}
              {(cfg.data?.sso || !cfg.data?.email_link) && (
                <Button
                  component="a"
                  href="/admin/v1/auth/login"
                  disabled={!cfg.data?.sso}
                  variant={cfg.data?.sso ? "filled" : "default"}
                  title={
                    cfg.data?.sso
                      ? undefined
                      : "SSO is not configured (CAPTAIN_ADMIN_OIDC)"
                  }
                >
                  Sign in with SSO
                </Button>
              )}
              {(cfg.data?.dev_token || cfg.data?.admin_key) && (
                <>
                  <Divider
                    label={cfg.data?.admin_key ? "Stixis admin key" : "development only"}
                    labelPosition="center"
                  />
                  <form
                    onSubmit={async (e) => {
                      e.preventDefault();
                      setBusy(true);
                      setToken(token.trim());
                      try {
                        await api.get<Me>("/me");
                        onSignedIn();
                      } catch (err) {
                        setToken(null);
                        notifyError(err);
                      } finally {
                        setBusy(false);
                      }
                    }}
                  >
                    <Stack>
                      <PasswordInput
                        label={cfg.data?.admin_key ? "Admin key" : "Dev admin token"}
                        description={
                          cfg.data?.admin_key
                            ? "The Stixis super admin key (CAPTAIN_PLATFORM_ADMIN_KEY)"
                            : "Accepted from this machine only"
                        }
                        value={token}
                        onChange={(e) => setTok(e.currentTarget.value)}
                        required
                      />
                      <Button
                        type="submit"
                        variant="default"
                        loading={busy}
                        disabled={token.trim().length < 32}
                      >
                        Sign in with token
                      </Button>
                    </Stack>
                  </form>
                </>
              )}
              {cfg.data &&
                !cfg.data.sso &&
                !cfg.data.dev_token &&
                !cfg.data.admin_key &&
                !cfg.data.email_link && (
                  <Text c="red">
                    No sign-in method is configured on the server.
                  </Text>
                )}
            </>
          )}
        </Stack>
      </Paper>
    </Center>
  );
}

export function App() {
  const qc = useQueryClient();
  const me = useMe();
  const authCfg = useQuery({
    queryKey: ["auth-config"],
    queryFn: () => api.get<AuthConfig>("/auth/config"),
    staleTime: Infinity,
  });
  const stixis = !!authCfg.data?.stixis_console;
  const stixisLoading = authCfg.isLoading;
  const [opened, { toggle, close }] = useDisclosure();
  const nav = useNavigate();
  const loc = useLocation();

  if (loc.pathname === "/accept-invite")
    return (
      <AcceptInvite
        onDone={() => void qc.invalidateQueries({ queryKey: ["me"] })}
      />
    );
  if (me.error instanceof ApiError && me.error.status === 401)
    return (
      <Login
        onSignedIn={() => void qc.invalidateQueries({ queryKey: ["me"] })}
      />
    );
  if (me.isLoading || !me.data || stixisLoading)
    return (
      <Center mih="100vh">
        <Loader aria-label="Loading" />
      </Center>
    );
  const allowed = NAV.filter(
    (n) =>
      can(me.data, n.needs) ||
      (n.path === "/approvals" && can(me.data, "audit:read")),
  );
  // Stixis console (control plane): only what a Stixis super admin needs. Everything else
  // governs a company's own AI use and lives in that company's Captain.
  const visible = stixis
    ? STIXIS_MENU.flatMap(({ path, label }) =>
        allowed.filter((n) => n.path === path).map((n) => ({ ...n, label })),
      )
    : allowed;

  return (
    <AppShell
      header={{ height: 56 }}
      navbar={{ width: 240, breakpoint: "sm", collapsed: { mobile: !opened } }}
      padding="lg"
    >
      <AppShell.Header>
        <Group h="100%" px="md" justify="space-between">
          <Group gap="xs">
            <Burger
              opened={opened}
              onClick={toggle}
              hiddenFrom="sm"
              size="sm"
              aria-label="Toggle navigation"
            />
            <IconShieldCheck size={22} />
            <Text fw={700}>Captain</Text>
            <Badge
              variant="light"
              size="sm"
              color={stixis ? "grape" : undefined}
            >
              {stixis ? "Stixis console" : "Console"}
            </Badge>
          </Group>
          <Group gap="xs">
            <Text size="sm" c="dimmed" visibleFrom="xs">
              {me.data.id} · {roleText(me.data.roles, stixis)}
            </Text>
            <Button
              size="xs"
              variant="subtle"
              leftSection={<IconLogout size={14} />}
              onClick={async () => {
                try {
                  if (!getToken()) await api.post("/auth/logout"); // ends the server-side SSO session
                } catch {
                  /* already signed out */
                }
                setToken(null);
                setCsrf(null);
                // reset (not clear): the mounted "me" query refetches, gets 401 and the
                // app shows the sign-in screen; all cached data from the session is dropped.
                await qc.resetQueries();
              }}
            >
              Sign out
            </Button>
          </Group>
        </Group>
      </AppShell.Header>
      <AppShell.Navbar p="xs">
        {visible.map((n) => (
          <NavLink
            key={n.path}
            label={n.label}
            leftSection={<n.icon size={18} />}
            active={loc.pathname === n.path}
            onClick={() => {
              nav(n.path);
              close();
            }}
          />
        ))}
      </AppShell.Navbar>
      <AppShell.Main>
        <Routes>
          {visible.map((n) => (
            <Route key={n.path} path={n.path} element={n.element} />
          ))}
          <Route
            path="*"
            element={<Navigate to={visible[0]?.path ?? "/"} replace />}
          />
        </Routes>
      </AppShell.Main>
    </AppShell>
  );
}
