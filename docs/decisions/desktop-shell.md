# Desktop shell decision (Stage 1)

**Status:** Tauri v2 selected for the first React/macOS prototype; packaged Python-host survival, updates, and distribution remain unqualified. This is a design decision, not evidence that a desktop application or research runtime exists.

## Work the shell must do

The shell presents a reviewable mission, receives user actions, and reconnects to a durable research host. It does not own the mission's scientific decisions or durable state. Closing a window or quitting the app must not silently cancel an active mission. A separate local Python host owns mission work and storage; a later cloud host can implement the same host/API boundary. A planned E2B process executes a bounded job and has neither the lifetime nor authority of that host. The interface is React in either shell.

The first target is the current Apple Silicon development Mac (macOS 15.5). The minimum supported release and Intel packaging remain decisions for a signed-package qualification pass; this record does not infer them from the development machine. Local inspection found Node 26.4, npm 11.17, Rust/Cargo 1.93, and Xcode Command Line Tools. Tauri's [macOS prerequisites](https://v2.tauri.app/start/prerequisites/) permit the command-line tools for desktop development; this does not prove this application's final packaging.

## Comparison

| Need | Tauri v2 | Electron | Decision effect |
| --- | --- | --- | --- |
| React rendering | Uses the [system webview and a Rust core](https://v2.tauri.app/concept/process-model/). WebKit behavior must be checked in the real app. | Bundles Chromium with a [main/renderer process split](https://www.electronjs.org/docs/latest/tutorial/process-model). More consistent rendering across Macs, at the cost of shipping its browser runtime. | React alone does not decide the shell. A first Mac build can qualify Tauri's actual WebKit behavior. |
| Packaged Python | [External binaries/sidecars](https://v2.tauri.app/develop/sidecar/) support packaged Python CLI/API executables and require a binary per target architecture. Shell permissions must be scoped. | The [main process](https://www.electronjs.org/docs/latest/tutorial/process-model) can launch a separate packaged Python executable. Its utility process is a Node process, not a Python mission host. | Both require explicit Python packaging, version handshake, logs, restart, and host supervision. Bundling a sidecar alone does not grant it an independent lifetime. |
| UI-to-host boundary | Tauri commands/capabilities can expose a narrow local client. Do not give web content general shell access. | Preload/context bridge and IPC can expose a narrow client; [Electron security guidance](https://www.electronjs.org/docs/latest/tutorial/security) calls for isolation and restricted navigation/content. | In either case, the React renderer gets only authenticated review/API operations, never raw provider keys or database writes. |
| Update and rollback | [Updater plugin](https://v2.tauri.app/plugin/updater/) supports signed update artifacts. Host schema/version compatibility still needs a separate plan. | [autoUpdater](https://www.electronjs.org/docs/latest/api/auto-updater) exists; macOS updates require a signed application. Host schema/version compatibility is likewise separate. | Neither updater is a migration or rollback solution by itself. Stage an app+host compatibility check and recoverable storage migration. |
| Diagnostics and developer cost | Rust plus Node build chain; smaller native wrapper, explicit capability configuration, and additional Rust packaging work. | Node-centric desktop tooling and bundled Chromium; larger privileged main-process surface to review. | The installed Rust/CLT makes Tauri plausible now. No measured app size, build time, crash rate, or installer outcome is claimed. |

**Why Tauri first:** it supplies a narrow desktop wrapper, explicit webview capabilities, and a documented path for bundling a Python executable on the selected local Mac. Its extra Rust layer is acceptable for this prototype because the toolchain is present. This is an inference from the product boundary and official documentation, not a measured Tauri-versus-Electron benchmark.

**When to revisit:** choose Electron if a real packaged Tauri build cannot reliably render the required review UI, launch/reconnect to the packaged Python host, expose adequate diagnostics, or update/rollback on the supported Mac target. If team maintenance of Rust proves materially costly, that is also a legitimate reversal reason. The same host/API/storage contracts should survive the shell change.

## Required process behavior

The Python mission host must run under an independent user-session supervisor or a cloud service and expose a local authenticated API. The shell may install/start the host through a controlled bootstrap, but it must discover an existing compatible host and reconnect after UI restart. A shell-owned child process that dies with the app is insufficient for unattended research. Choose and test the exact Mac supervisor and package handoff during implementation; none is assumed to exist today.

| Event | Expected mission behavior | Proof still needed |
| --- | --- | --- |
| Window close | Hide review surface; host retains mission and accepted state. | Packaged-app close/reopen while a synthetic mission progresses. |
| App quit | UI and its connection end; independently supervised host continues or reports a deliberate host policy. | Quit/relaunch, identify the same mission and cursor, no duplicate host. |
| Host crash | Durable commits remain; supervisor restarts host; leases/uncertain effects reconcile before retry. | Kill host between file write and metadata commit and during a queued operation. |
| System sleep | No work is presumed to run while asleep; after wake, timeouts/leases are reconciled without false completion. | Sleep/wake test with active and pending work. |
| Explicit mission cancel | Authenticated user command records cancellation, propagates to admitted work, preserves completed artifacts and uncertain external-effect receipts. | Cancel during a local operation and a planned external sandbox job. |
| Cloud research host | Its service and storage lifetime are independent of the Mac shell and local Codex development task. | Remote reconnect/auth, data placement and recovery test when deployed. |
| Codex development task | May edit and test code; has no automatic control over a research mission. | Contract review; no runtime inheritance. |
| E2B job | Bounded execution attempt; returns a result/receipt to the research host, not a new mission owner. | Later live sandbox lifecycle and result reconciliation. |

## Security and update contract

The shell keeps only an OS-protected local client credential or short-lived authenticated session, not research-provider secrets. The host stores provider credentials outside the renderer and passes minimum scoped inputs to adapters and sandbox jobs. The API rejects a UI request with an unknown mission, stale version, or insufficient authority. Scientific papers/web content are untrusted data and are never allowed to invoke desktop commands. Reader reports omit raw traces and restricted provider-returned reasoning; the review API serves such diagnostics only to an authorized view.

Before app or host update: record installed app/host/schema versions, make a verified backup, check compatible protocol versions, perform an idempotent migration, and retain a recovery path. The app updater's ability to download/install a bundle is not proof of a safe host/schema rollback. Signed distribution, notarization, exact minimum macOS target, Intel/universal build, installer repair, and updater recovery remain untested.

## Stage 1 evidence and next qualification

The shell comparison is based on the linked vendor documentation and local command output, not a built package. Stage 1 can proceed with a React sample-data view and an API stub. The next shell-specific proof is one packaged Apple Silicon app that reconnects to a separately supervised synthetic host after window close, app quit, host restart, and update failure; check its rendered WebKit UI and diagnostics in the actual Mac app. Do not mark the lifecycle behavior implemented from this record alone.
