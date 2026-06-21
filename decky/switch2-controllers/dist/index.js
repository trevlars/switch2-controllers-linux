const manifest = {"name":"Switch 2 Controllers"};
const API_VERSION = 2;
const internalAPIConnection = window.__DECKY_SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED_deckyLoaderAPIInit;
if (!internalAPIConnection) {
    throw new Error('[@decky/api]: Failed to connect to the loader as as the loader API was not initialized. This is likely a bug in Decky Loader.');
}
let api;
try {
    api = internalAPIConnection.connect(API_VERSION, manifest.name);
}
catch {
    api = internalAPIConnection.connect(1, manifest.name);
    console.warn(`[@decky/api] Requested API version ${API_VERSION} but the running loader only supports version 1. Some features may not work.`);
}
if (api._version != API_VERSION) {
    console.warn(`[@decky/api] Requested API version ${API_VERSION} but the running loader only supports version ${api._version}. Some features may not work.`);
}
const callable = api.callable;
const toaster = api.toaster;
const definePlugin = (fn) => {
    return (...args) => {
        return fn(...args);
    };
};

var DefaultContext = {
  color: undefined,
  size: undefined,
  className: undefined,
  style: undefined,
  attr: undefined
};
var IconContext = SP_REACT.createContext && /*#__PURE__*/SP_REACT.createContext(DefaultContext);

var _excluded = ["attr", "size", "title"];
function _objectWithoutProperties(e, t) { if (null == e) return {}; var o, r, i = _objectWithoutPropertiesLoose(e, t); if (Object.getOwnPropertySymbols) { var n = Object.getOwnPropertySymbols(e); for (r = 0; r < n.length; r++) o = n[r], -1 === t.indexOf(o) && {}.propertyIsEnumerable.call(e, o) && (i[o] = e[o]); } return i; }
function _objectWithoutPropertiesLoose(r, e) { if (null == r) return {}; var t = {}; for (var n in r) if ({}.hasOwnProperty.call(r, n)) { if (-1 !== e.indexOf(n)) continue; t[n] = r[n]; } return t; }
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
function ownKeys(e, r) { var t = Object.keys(e); if (Object.getOwnPropertySymbols) { var o = Object.getOwnPropertySymbols(e); r && (o = o.filter(function (r) { return Object.getOwnPropertyDescriptor(e, r).enumerable; })), t.push.apply(t, o); } return t; }
function _objectSpread(e) { for (var r = 1; r < arguments.length; r++) { var t = null != arguments[r] ? arguments[r] : {}; r % 2 ? ownKeys(Object(t), true).forEach(function (r) { _defineProperty(e, r, t[r]); }) : Object.getOwnPropertyDescriptors ? Object.defineProperties(e, Object.getOwnPropertyDescriptors(t)) : ownKeys(Object(t)).forEach(function (r) { Object.defineProperty(e, r, Object.getOwnPropertyDescriptor(t, r)); }); } return e; }
function _defineProperty(e, r, t) { return (r = _toPropertyKey(r)) in e ? Object.defineProperty(e, r, { value: t, enumerable: true, configurable: true, writable: true }) : e[r] = t, e; }
function _toPropertyKey(t) { var i = _toPrimitive(t, "string"); return "symbol" == typeof i ? i : i + ""; }
function _toPrimitive(t, r) { if ("object" != typeof t || !t) return t; var e = t[Symbol.toPrimitive]; if (void 0 !== e) { var i = e.call(t, r); if ("object" != typeof i) return i; throw new TypeError("@@toPrimitive must return a primitive value."); } return ("string" === r ? String : Number)(t); }
function Tree2Element(tree) {
  return tree && tree.map((node, i) => /*#__PURE__*/SP_REACT.createElement(node.tag, _objectSpread({
    key: i
  }, node.attr), Tree2Element(node.child)));
}
function GenIcon(data) {
  return props => /*#__PURE__*/SP_REACT.createElement(IconBase, _extends({
    attr: _objectSpread({}, data.attr)
  }, props), Tree2Element(data.child));
}
function IconBase(props) {
  var elem = conf => {
    var {
        attr,
        size,
        title
      } = props,
      svgProps = _objectWithoutProperties(props, _excluded);
    var computedSize = size || conf.size || "1em";
    var className;
    if (conf.className) className = conf.className;
    if (props.className) className = (className ? className + " " : "") + props.className;
    return /*#__PURE__*/SP_REACT.createElement("svg", _extends({
      stroke: "currentColor",
      fill: "currentColor",
      strokeWidth: "0"
    }, conf.attr, attr, svgProps, {
      className: className,
      style: _objectSpread(_objectSpread({
        color: props.color || conf.color
      }, conf.style), props.style),
      height: computedSize,
      width: computedSize,
      xmlns: "http://www.w3.org/2000/svg"
    }), title && /*#__PURE__*/SP_REACT.createElement("title", null, title), props.children);
  };
  return IconContext !== undefined ? /*#__PURE__*/SP_REACT.createElement(IconContext.Consumer, null, conf => elem(conf)) : elem(DefaultContext);
}

// THIS FILE IS AUTO GENERATED
function FaGamepad (props) {
  return GenIcon({"attr":{"viewBox":"0 0 640 512"},"child":[{"tag":"path","attr":{"d":"M480.07 96H160a160 160 0 1 0 114.24 272h91.52A160 160 0 1 0 480.07 96zM248 268a12 12 0 0 1-12 12h-52v52a12 12 0 0 1-12 12h-24a12 12 0 0 1-12-12v-52H84a12 12 0 0 1-12-12v-24a12 12 0 0 1 12-12h52v-52a12 12 0 0 1 12-12h24a12 12 0 0 1 12 12v52h52a12 12 0 0 1 12 12zm216 76a40 40 0 1 1 40-40 40 40 0 0 1-40 40zm64-96a40 40 0 1 1 40-40 40 40 0 0 1-40 40z"},"child":[]}]})(props);
}

const getStatus = callable("get_status");
const ensureBridge = callable("ensure_bridge");
const addController = callable("add_controller");
const removeController = callable("remove_controller");
const repairController = callable("repair_controller");
const swapPlayers = callable("swap_players");
const rebond = callable("rebond");
const restartBridge = callable("restart_bridge");
const getLogs = callable("get_logs");
async function toastResult(title, result) {
    toaster.toast({
        title,
        body: result.ok
            ? result.message?.slice(0, 180) || "Done"
            : result.message?.slice(0, 220) || "Something went wrong",
        duration: result.ok ? 3500 : 6000,
    });
}
function Content() {
    const [status, setStatus] = SP_REACT.useState(null);
    const [busy, setBusy] = SP_REACT.useState(false);
    const refresh = SP_REACT.useCallback(async () => {
        try {
            setStatus(await getStatus());
        }
        catch (e) {
            console.error(e);
        }
    }, []);
    SP_REACT.useEffect(() => {
        refresh();
        const id = setInterval(refresh, 2000);
        return () => clearInterval(id);
    }, [refresh]);
    const run = async (title, fn, hint) => {
        if (busy)
            return;
        setBusy(true);
        if (hint) {
            toaster.toast({ title, body: hint, duration: 5000 });
        }
        try {
            const result = await fn();
            await toastResult(title, result);
            if (result.status)
                setStatus(result.status);
            else
                await refresh();
        }
        finally {
            setBusy(false);
        }
    };
    const pads = status?.pads ?? [];
    const bridgeOn = status?.service === "active";
    return (SP_JSX.jsxs(SP_JSX.Fragment, { children: [SP_JSX.jsxs(DFL.PanelSection, { title: "Status", children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsxs("div", { style: { display: "flex", flexDirection: "column", gap: 6 }, children: [SP_JSX.jsx("div", { style: { fontSize: "1.15em", fontWeight: 600 }, children: status?.headline ?? "Loading…" }), SP_JSX.jsx("div", { style: { opacity: 0.75, fontSize: "0.92em" }, children: status?.detail ?? "" }), SP_JSX.jsxs("div", { style: { opacity: 0.55, fontSize: "0.85em" }, children: ["Bridge: ", bridgeOn ? "running" : "stopped", status ? ` · ${status.connected_count}/${pads.length} connected` : ""] })] }) }), !bridgeOn && (SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: busy, onClick: () => run("Bridge started", ensureBridge), children: "Start Bridge" }) }))] }), pads.map((pad) => (SP_JSX.jsxs(DFL.PanelSection, { title: `Player ${pad.player} · ${pad.name}`, children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { style: { opacity: 0.8 }, children: pad.status }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: busy, onClick: () => run("Re-bonding", rebond, `Hold Sync on ${pad.name}, then confirm in toast flow`), children: "Re-bond This Pad" }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: busy, onClick: () => run("Set up again", () => repairController(pad.mac, pad.player), `Hold Sync on ${pad.name} now`), children: "Remove & Set Up Again" }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: busy, onClick: () => run("Removed", () => removeController(pad.mac)), children: "Remove Controller" }) })] }, pad.mac))), SP_JSX.jsxs(DFL.PanelSection, { title: "Actions", children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: busy, onClick: () => run("Adding controller", addController, "Hold Sync on the NEW pad now"), children: "Add Controller" }) }), pads.length >= 2 && (SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: busy, onClick: () => run("Players swapped", swapPlayers), children: "Swap Player 1 \u2194 2" }) })), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: busy, onClick: () => run("Bridge restarted", restartBridge), children: "Restart Bridge" }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: busy, onClick: async () => {
                                if (busy)
                                    return;
                                setBusy(true);
                                try {
                                    const logs = await getLogs();
                                    toaster.toast({ title: "Recent logs", body: logs.slice(-900), duration: 8000 });
                                }
                                finally {
                                    setBusy(false);
                                }
                            }, children: "View Logs" }) })] }), SP_JSX.jsx(DFL.PanelSection, { title: "Help", children: SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { style: { opacity: 0.65, fontSize: "0.88em" }, children: "Hold Sync to connect each pad. Add each controller once; swap P1/P2 here without leaving Game Mode." }) }) })] }));
}
var index = definePlugin(() => ({
    name: "Switch 2 Controllers",
    titleView: SP_JSX.jsx("div", { className: DFL.staticClasses.Title, children: "Switch 2 Controllers" }),
    content: SP_JSX.jsx(Content, {}),
    icon: SP_JSX.jsx(FaGamepad, {}),
}));

export { index as default };
//# sourceMappingURL=index.js.map
