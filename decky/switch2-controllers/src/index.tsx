import {
  ButtonItem,
  PanelSection,
  PanelSectionRow,
  staticClasses,
} from "@decky/ui";
import { callable, definePlugin, toaster } from "@decky/api";
import { useCallback, useEffect, useState } from "react";
import { FaGamepad } from "react-icons/fa";

type Pad = {
  player: number;
  name: string;
  mac: string;
  bonded: boolean;
  connected: boolean;
  battery_pct?: number | null;
  status: string;
};

type Status = {
  service: string;
  headline: string;
  detail: string;
  connected_count: number;
  pads: Pad[];
};

type ActionResult = { ok: boolean; message?: string; status?: Status };

const getStatus = callable<[], Status>("get_status");
const ensureBridge = callable<[], ActionResult>("ensure_bridge");
const addController = callable<[], ActionResult>("add_controller");
const removeController = callable<[mac: string], ActionResult>("remove_controller");
const repairController = callable<[mac: string, player: number], ActionResult>("repair_controller");
const swapPlayers = callable<[], ActionResult>("swap_players");
const rebond = callable<[], ActionResult>("rebond");
const restartBridge = callable<[], ActionResult>("restart_bridge");
const getLogs = callable<[], string>("get_logs");

async function toastResult(title: string, result: ActionResult) {
  toaster.toast({
    title,
    body: result.ok
      ? result.message?.slice(0, 180) || "Done"
      : result.message?.slice(0, 220) || "Something went wrong",
    duration: result.ok ? 3500 : 6000,
  });
}

function Content() {
  const [status, setStatus] = useState<Status | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setStatus(await getStatus());
    } catch (e) {
      console.error(e);
    }
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 2000);
    return () => clearInterval(id);
  }, [refresh]);

  const run = async (title: string, fn: () => Promise<ActionResult>, hint?: string) => {
    if (busy) return;
    setBusy(true);
    if (hint) {
      toaster.toast({ title, body: hint, duration: 5000 });
    }
    try {
      const result = await fn();
      await toastResult(title, result);
      if (result.status) setStatus(result.status);
      else await refresh();
    } finally {
      setBusy(false);
    }
  };

  const pads = status?.pads ?? [];
  const bridgeOn = status?.service === "active";

  return (
    <>
      <PanelSection title="Status">
        <PanelSectionRow>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            <div style={{ fontSize: "1.15em", fontWeight: 600 }}>
              {status?.headline ?? "Loading…"}
            </div>
            <div style={{ opacity: 0.75, fontSize: "0.92em" }}>
              {status?.detail ?? ""}
            </div>
            <div style={{ opacity: 0.55, fontSize: "0.85em" }}>
              Bridge: {bridgeOn ? "running" : "stopped"}
              {status ? ` · ${status.connected_count}/${pads.length} connected` : ""}
            </div>
          </div>
        </PanelSectionRow>
        {!bridgeOn && (
          <PanelSectionRow>
            <ButtonItem layout="below" disabled={busy} onClick={() => run("Bridge started", ensureBridge)}>
              Start Bridge
            </ButtonItem>
          </PanelSectionRow>
        )}
      </PanelSection>

      {pads.map((pad) => (
        <PanelSection key={pad.mac} title={`Player ${pad.player} · ${pad.name}`}>
          <PanelSectionRow>
            <div style={{ opacity: 0.8 }}>{pad.status}</div>
          </PanelSectionRow>
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              disabled={busy}
              onClick={() =>
                run("Re-bonding", rebond, `Hold Sync on ${pad.name}, then confirm in toast flow`)
              }
            >
              Re-bond This Pad
            </ButtonItem>
          </PanelSectionRow>
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              disabled={busy}
              onClick={() =>
                run(
                  "Set up again",
                  () => repairController(pad.mac, pad.player),
                  `Hold Sync on ${pad.name} now`
                )
              }
            >
              Remove & Set Up Again
            </ButtonItem>
          </PanelSectionRow>
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              disabled={busy}
              onClick={() =>
                run("Removed", () => removeController(pad.mac))
              }
            >
              Remove Controller
            </ButtonItem>
          </PanelSectionRow>
        </PanelSection>
      ))}

      <PanelSection title="Actions">
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={busy}
            onClick={() => run("Adding controller", addController, "Hold Sync on the NEW pad now")}
          >
            Add Controller
          </ButtonItem>
        </PanelSectionRow>
        {pads.length >= 2 && (
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              disabled={busy}
              onClick={() => run("Players swapped", swapPlayers)}
            >
              Swap Player 1 ↔ 2
            </ButtonItem>
          </PanelSectionRow>
        )}
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={busy}
            onClick={() => run("Bridge restarted", restartBridge)}
          >
            Restart Bridge
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={busy}
            onClick={async () => {
              if (busy) return;
              setBusy(true);
              try {
                const logs = await getLogs();
                toaster.toast({ title: "Recent logs", body: logs.slice(-900), duration: 8000 });
              } finally {
                setBusy(false);
              }
            }}
          >
            View Logs
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Help">
        <PanelSectionRow>
          <div style={{ opacity: 0.65, fontSize: "0.88em" }}>
            Hold Sync to connect each pad. Add each controller once; swap P1/P2 here
            without leaving Game Mode.
          </div>
        </PanelSectionRow>
      </PanelSection>
    </>
  );
}

export default definePlugin(() => ({
  name: "Switch 2 Controllers",
  titleView: <div className={staticClasses.Title}>Switch 2 Controllers</div>,
  content: <Content />,
  icon: <FaGamepad />,
}));
