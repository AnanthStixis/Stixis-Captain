import { Alert, AppShell, Badge, Burger, Button, Center, Divider, Group, Loader, NavLink, Paper, PasswordInput, Stack, Text, Title } from "@mantine/core";
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
  IconGauge,
  IconKey,
  IconLogout,
  IconPower,
  IconScale,
  IconShieldCheck,
  type Icon,
} from "@tabler/icons-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";

import { api, ApiError, getToken, setCsrf, setToken, type AuthConfig, type Me, type SessionInfo } from "./api";
import { notifyError } from "./components/common";
import { AlertsPage } from "./pages/Alerts";
import { ApprovalsPage } from "./pages/Approvals";
import { AuditPage } from "./pages/Audit";
import { BudgetsPage } from "./pages/Budgets";
import { EventsPage } from "./pages/Events";
import { KeysPage } from "./pages/Keys";
import { KillSwitchPage } from "./pages/KillSwitch";
import { AgentsPage } from "./pages/Agents";
import { CoveragePage } from "./pages/Coverage";
import { ObservedPage } from "./pages/Observed";
import { PolicyPage } from "./pages/Policy";
import { OverviewPage } from "./pages/Overview";
import { RegisterPage } from "./pages/Register";
import { RegistryPage } from "./pages/Registry";
import { UsageBreakdownPage } from "./pages/UsageBreakdown";

interface NavItem {
  path: string;
  label: string;
  icon: Icon;
  needs: string; // permission required to see the page
  element: JSX.Element;
}

export const NAV: NavItem[] = [
  { path: "/", label: "Overview", icon: IconGauge, needs: "usage:read", element: <OverviewPage /> },
  { path: "/activity", label: "Activity", icon: IconActivity, needs: "usage:read", element: <EventsPage /> },
  { path: "/usage", label: "Usage by…", icon: IconUsers, needs: "usage:read", element: <UsageBreakdownPage /> },
  { path: "/coverage", label: "Coverage", icon: IconChartPie, needs: "usage:read", element: <CoveragePage /> },
  { path: "/agents", label: "Agents & systems", icon: IconRobot, needs: "register:read", element: <AgentsPage /> },
  { path: "/observed", label: "Observed usage", icon: IconEye, needs: "usage:read", element: <ObservedPage /> },
  { path: "/approvals", label: "Approvals", icon: IconChecklist, needs: "approval:decide", element: <ApprovalsPage /> },
  { path: "/budgets", label: "Budgets", icon: IconCash, needs: "budget:read", element: <BudgetsPage /> },
  { path: "/keys", label: "Keys", icon: IconKey, needs: "keys:read", element: <KeysPage /> },
  { path: "/register", label: "AI Register", icon: IconBook2, needs: "register:read", element: <RegisterPage /> },
  { path: "/registry", label: "Models & tools", icon: IconBox, needs: "register:read", element: <RegistryPage /> },
  { path: "/policy", label: "Policy", icon: IconScale, needs: "usage:read", element: <PolicyPage /> },
  { path: "/killswitch", label: "Kill switch", icon: IconPower, needs: "usage:read", element: <KillSwitchPage /> },
  { path: "/alerts", label: "Alerts", icon: IconAlertTriangle, needs: "usage:read", element: <AlertsPage /> },
  { path: "/audit", label: "Audit & reports", icon: IconShieldCheck, needs: "audit:read", element: <AuditPage /> },
];

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
  no_role: "You signed in, but no Captain role is assigned to your groups. Ask your administrator for access.",
  denied: "Sign-in was cancelled or denied at your identity provider.",
  state: "The sign-in session expired or could not be verified. Please try again.",
  groups_overage: "You belong to too many groups for the sign-in token. Ask your administrator to send Captain app roles, or only the Captain groups.",
  failed: "Single sign-on could not be completed. Please try again, or contact your administrator.",
};

function Login({ onSignedIn }: { onSignedIn: () => void }) {
  const [token, setTok] = useState("");
  const [busy, setBusy] = useState(false);
  const cfg = useQuery({ queryKey: ["auth-config"], queryFn: () => api.get<AuthConfig>("/auth/config"), retry: 1 });
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
            <Title order={2}>Captain Console</Title>
            <Text c="dimmed" size="sm">
              Central control for AI governance
            </Text>
          </div>
          {ssoError && (
            <Alert color="red" icon={<IconAlertTriangle size={16} />} role="alert">
              {ssoError}
            </Alert>
          )}
          {cfg.isLoading ? (
            <Center>
              <Loader size="sm" aria-label="Loading" />
            </Center>
          ) : (
            <>
              <Button
                component="a"
                href="/admin/v1/auth/login"
                disabled={!cfg.data?.sso}
                variant={cfg.data?.sso ? "filled" : "default"}
                title={cfg.data?.sso ? undefined : "SSO is not configured (CAPTAIN_ADMIN_OIDC)"}
              >
                Sign in with SSO
              </Button>
              {cfg.data?.dev_token && (
                <>
                  <Divider label="development only" labelPosition="center" />
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
                      <PasswordInput label="Dev admin token" description="Accepted from this machine only" value={token} onChange={(e) => setTok(e.currentTarget.value)} required />
                      <Button type="submit" variant="default" loading={busy} disabled={token.trim().length < 32}>
                        Sign in with token
                      </Button>
                    </Stack>
                  </form>
                </>
              )}
              {cfg.data && !cfg.data.sso && !cfg.data.dev_token && <Text c="red">No sign-in method is configured on the server.</Text>}
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
  const [opened, { toggle, close }] = useDisclosure();
  const nav = useNavigate();
  const loc = useLocation();

  if (me.error instanceof ApiError && me.error.status === 401)
    return <Login onSignedIn={() => void qc.invalidateQueries({ queryKey: ["me"] })} />;
  if (me.isLoading || !me.data)
    return (
      <Center mih="100vh">
        <Loader aria-label="Loading" />
      </Center>
    );
  const visible = NAV.filter((n) => can(me.data, n.needs) || (n.path === "/approvals" && can(me.data, "audit:read")));

  return (
    <AppShell header={{ height: 56 }} navbar={{ width: 240, breakpoint: "sm", collapsed: { mobile: !opened } }} padding="lg">
      <AppShell.Header>
        <Group h="100%" px="md" justify="space-between">
          <Group gap="xs">
            <Burger opened={opened} onClick={toggle} hiddenFrom="sm" size="sm" aria-label="Toggle navigation" />
            <IconShieldCheck size={22} />
            <Text fw={700}>Captain</Text>
            <Badge variant="light" size="sm">
              Console
            </Badge>
          </Group>
          <Group gap="xs">
            <Text size="sm" c="dimmed" visibleFrom="xs">
              {me.data.id} · {me.data.roles.join(", ")}
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
          <Route path="*" element={<Navigate to={visible[0]?.path ?? "/"} replace />} />
        </Routes>
      </AppShell.Main>
    </AppShell>
  );
}
