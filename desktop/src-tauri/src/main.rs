//! Open the bundled sample or an explicitly launched local research connection.
//! The Python host owns research execution and credentials. Closing this window
//! leaves that separate process and its saved work running.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::{
    fs::{self, OpenOptions},
    io::Write,
    path::{Path, PathBuf},
    process::Command,
    sync::{
        atomic::{AtomicU64, Ordering},
        Mutex,
    },
};
use tauri::Manager;

const CONNECTION_FILE: &str = "selected-live-workspace";
const RECONNECT_MENU_ID: &str = "reconnect-saved-workspace";
static DRAFT_WRITE_SEQUENCE: AtomicU64 = AtomicU64::new(0);

#[derive(Clone)]
struct DraftAccess {
    state_dir: PathBuf,
    origin: String,
    workspace_id: String,
}

fn valid_workspace_id(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 80
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-' || byte == b'_')
}

fn draft_access_from_connection(
    state_dir: &Path,
    verified_url: &tauri::Url,
    contents: &str,
) -> Option<DraftAccess> {
    let connection: serde_json::Value = serde_json::from_str(contents).ok()?;
    let origin = connection.get("origin")?.as_str()?;
    let token = connection.get("token")?.as_str()?;
    let launch_url = connection.get("launchUrl")?.as_str()?;
    let workspace_id = connection.get("workspaceId")?.as_str()?;
    if !valid_workspace_id(workspace_id)
        || token.is_empty()
        || !token
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-' || byte == b'_')
        || launch_url != verified_url.as_str()
        || launch_url != format!("{origin}/live#token={token}")
    {
        return None;
    }
    let origin_url = tauri::Url::parse(origin).ok()?;
    if origin_url.scheme() != "http"
        || origin_url.host_str() != Some("127.0.0.1")
        || origin_url.port().is_none()
        || origin_url.path() != "/"
        || origin_url.query().is_some()
        || origin_url.fragment().is_some()
        || origin_url.origin() != verified_url.origin()
    {
        return None;
    }
    Some(DraftAccess {
        state_dir: state_dir.to_path_buf(),
        origin: origin_url.origin().ascii_serialization(),
        workspace_id: workspace_id.to_owned(),
    })
}

fn authenticated_draft_access(state_dir: &Path, verified_url: &tauri::Url) -> Option<DraftAccess> {
    let contents = fs::read_to_string(state_dir.join("connection.json")).ok()?;
    draft_access_from_connection(state_dir, verified_url, &contents)
}

fn set_draft_access(window: &tauri::WebviewWindow, access: Option<DraftAccess>) {
    if let Ok(mut current) = window
        .app_handle()
        .state::<Mutex<Option<DraftAccess>>>()
        .lock()
    {
        *current = access;
    }
}

fn allows_draft_access(
    access: &DraftAccess,
    current_url: &tauri::Url,
    selected_dir: &Path,
    workspace_id: &str,
) -> bool {
    current_url.scheme() == "http"
        && current_url.host_str() == Some("127.0.0.1")
        && current_url.path() == "/live"
        && current_url.origin().ascii_serialization() == access.origin
        && selected_dir == access.state_dir
        && workspace_id == access.workspace_id
}

fn selection_file(window: &tauri::WebviewWindow) -> Result<PathBuf, String> {
    let directory = window
        .app_handle()
        .path()
        .app_config_dir()
        .map_err(|_| "Could not locate the Atlas settings directory.")?;
    fs::create_dir_all(&directory).map_err(|_| "Could not create the Atlas settings directory.")?;
    Ok(directory.join(CONNECTION_FILE))
}

fn selected_state_dir(window: &tauri::WebviewWindow) -> Option<PathBuf> {
    let value = fs::read_to_string(selection_file(window).ok()?).ok()?;
    let path = PathBuf::from(value.trim());
    path.is_absolute().then_some(path)
}

fn save_state_dir(window: &tauri::WebviewWindow, directory: &Path) -> Result<(), String> {
    let path = selection_file(window)?;
    let temporary = path.with_extension(format!(
        "{}.tmp",
        DRAFT_WRITE_SEQUENCE.fetch_add(1, Ordering::Relaxed)
    ));
    fs::write(&temporary, directory.to_string_lossy().as_bytes())
        .map_err(|_| "Could not save the selected workspace.")?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&temporary, fs::Permissions::from_mode(0o600))
            .map_err(|_| "Could not protect the selected workspace setting.")?;
    }
    fs::rename(temporary, path).map_err(|_| "Could not save the selected workspace.".to_string())
}

fn draft_file(window: &tauri::WebviewWindow, workspace_id: &str) -> Result<PathBuf, String> {
    if !valid_workspace_id(workspace_id) {
        return Err("Invalid saved workspace identity.".into());
    }
    let current_url = window
        .url()
        .map_err(|_| "The research page is unavailable.")?;
    let selected_dir = selected_state_dir(window)
        .ok_or("No authenticated local research workspace is selected.")?;
    let current = window.app_handle().state::<Mutex<Option<DraftAccess>>>();
    let access = current
        .lock()
        .map_err(|_| "Local draft access is unavailable.")?;
    if !access
        .as_ref()
        .is_some_and(|proof| allows_draft_access(proof, &current_url, &selected_dir, workspace_id))
    {
        return Err("This page cannot access the selected workspace draft.".into());
    }
    Ok(selection_file(window)?.with_file_name(format!("live-draft-{workspace_id}")))
}

#[tauri::command]
fn load_live_draft(
    window: tauri::WebviewWindow,
    workspace_id: &str,
) -> Result<Option<String>, String> {
    let path = draft_file(&window, workspace_id)?;
    match fs::read_to_string(path) {
        Ok(value) => Ok(Some(value)),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(_) => Err("Could not read the local research draft.".into()),
    }
}

#[tauri::command]
fn save_live_draft(
    window: tauri::WebviewWindow,
    workspace_id: &str,
    draft: &str,
) -> Result<(), String> {
    if draft.len() > 4_000_000 {
        return Err("The draft is too large to save locally.".into());
    }
    let path = draft_file(&window, workspace_id)?;
    let temporary = path.with_extension(format!(
        "{}.{}.tmp",
        std::process::id(),
        DRAFT_WRITE_SEQUENCE.fetch_add(1, Ordering::Relaxed)
    ));
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut file = options
        .open(&temporary)
        .map_err(|_| "Could not create the local research draft.")?;
    file.write_all(draft.as_bytes())
        .and_then(|_| file.sync_all())
        .map_err(|_| "Could not save the local research draft.")?;
    drop(file);
    fs::rename(temporary, path).map_err(|_| "Could not save the local research draft.".to_string())
}

fn validated_connection(directory: &Path) -> Result<tauri::Url, String> {
    let repo = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .and_then(Path::parent)
        .ok_or("Could not locate the local Atlas checkout.")?;
    let cli = repo.join(".venv/bin/ora");
    let output = Command::new(cli)
        .current_dir(repo)
        .args(["live", "connect", "--state-dir"])
        .arg(directory)
        .output()
        .map_err(|_| "The local Atlas connection helper is unavailable.")?;
    if !output.status.success() {
        return Err("The selected workspace is unavailable or its local host could not be verified. Check the directory and host, then retry.".into());
    }
    let value =
        String::from_utf8(output.stdout).map_err(|_| "The local connection record is invalid.")?;
    let url = live_url(value.trim())?;
    if !url
        .fragment()
        .is_some_and(|part| part.starts_with("token=") && part.len() > 6)
    {
        return Err("The local connection has no valid viewer session.".into());
    }
    Ok(url)
}

fn navigate_live(
    window: &tauri::WebviewWindow,
    directory: &Path,
    view: &str,
    run_id: Option<&str>,
) -> Result<(), String> {
    if !matches!(view, "request" | "history") {
        return Err("Unknown live workspace view.".into());
    }
    let mut url = validated_connection(directory)?;
    let access = authenticated_draft_access(directory, &url);
    url.query_pairs_mut().append_pair("view", view);
    if let Some(id) = run_id.filter(|id| !id.is_empty() && id.len() <= 256) {
        url.query_pairs_mut().append_pair("runId", id);
    }
    save_state_dir(window, directory)?;
    set_draft_access(window, access);
    window
        .navigate(url)
        .map_err(|_| "Could not open the selected local workspace.".to_string())
}

fn live_url(value: &str) -> Result<tauri::Url, String> {
    let url = tauri::Url::parse(value).map_err(|_| "Invalid local research connection.")?;
    if url.scheme() != "http"
        || url.host_str() != Some("127.0.0.1")
        || url.port().is_none()
        || url.path() != "/live"
        || !url.username().is_empty()
        || url.password().is_some()
        || url.query().is_some()
    {
        return Err("Live research must use the explicit local host".into());
    }
    Ok(url)
}

fn selected_run_from_url(url: &tauri::Url) -> Option<String> {
    if url.scheme() != "http"
        || url.host_str() != Some("127.0.0.1")
        || url.port().is_none()
        || url.path() != "/live"
        || !url.username().is_empty()
        || url.password().is_some()
    {
        return None;
    }
    let mut matches = url.query_pairs().filter(|(key, _)| key == "runId");
    let id = matches.next()?.1.into_owned();
    (matches.next().is_none()
        && !id.is_empty()
        && id.len() <= 256
        && id
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-' || byte == b'_'))
    .then_some(id)
}

fn reconnect_saved_workspace(window: &tauri::WebviewWindow) -> Result<(), String> {
    let directory = selected_state_dir(window)
        .ok_or("No saved local workspace is selected. Reopen it from the recorded view first.")?;
    let run_id = window
        .url()
        .ok()
        .and_then(|url| selected_run_from_url(&url));
    navigate_live(window, &directory, "history", run_id.as_deref())
}

#[tauri::command]
fn open_live_workspace(
    window: tauri::WebviewWindow,
    view: &str,
    run_id: Option<&str>,
) -> Result<(), String> {
    if !matches!(view, "request" | "history") {
        return Err("Unknown live workspace view.".into());
    }
    if let (Ok(active), Some(selected)) = (
        std::env::var("ORA_LIVE_STATE_DIR"),
        selected_state_dir(&window),
    ) {
        if fs::canonicalize(active).ok().as_deref() == Some(selected.as_path()) {
            return navigate_live(&window, &selected, view, run_id);
        }
    }
    if let Ok(value) = std::env::var("ORA_LIVE_URL") {
        let mut url = live_url(&value)?;
        url.query_pairs_mut().append_pair("view", view);
        if let Some(id) = run_id.filter(|id| !id.is_empty() && id.len() <= 256) {
            url.query_pairs_mut().append_pair("runId", id);
        }
        set_draft_access(&window, None);
        return window
            .navigate(url)
            .map_err(|_| "Could not open the local live workspace.".into());
    }
    let directory = selected_state_dir(&window)
        .ok_or("Select the saved local workspace directory to reconnect.")?;
    navigate_live(&window, &directory, view, run_id)
}

#[tauri::command]
fn connect_live_workspace(
    window: tauri::WebviewWindow,
    state_dir: &str,
    view: &str,
    run_id: Option<&str>,
) -> Result<(), String> {
    let directory = fs::canonicalize(state_dir.trim())
        .map_err(|_| "Choose an existing saved local workspace directory.")?;
    if !directory.is_dir() {
        return Err("Choose an existing saved local workspace directory.".into());
    }
    navigate_live(&window, &directory, view, run_id)
}

#[tauri::command]
fn get_live_workspace_selection(window: tauri::WebviewWindow) -> Option<String> {
    selected_state_dir(&window).map(|path| path.to_string_lossy().into_owned())
}

#[tauri::command]
fn set_atlas_view_title(window: tauri::WebviewWindow, mode: &str) -> Result<(), String> {
    let title = match mode {
        "live" => "Research Atlas · Live local research",
        "recorded" => "Research Atlas · Recorded offline test",
        "sample" => "Research Atlas · Sample prototype",
        _ => return Err("Unknown Atlas view mode.".into()),
    };
    window
        .set_title(title)
        .map_err(|_| "Could not update the window title.".into())
}

fn main() {
    tauri::Builder::default()
        .manage(Mutex::new(None::<DraftAccess>))
        .on_menu_event(|app, event| {
            if event.id() != RECONNECT_MENU_ID {
                return;
            }
            if let Some(window) = app.get_webview_window("main") {
                if let Err(error) = reconnect_saved_workspace(&window) {
                    eprintln!("Atlas reconnect failed: {error}");
                    let _ = window.set_title("Research Atlas · Reconnect failed; check local host");
                }
            }
        })
        .on_page_load(|webview, payload| {
            // Keep the native title accurate even when a localhost page cannot
            // invoke shell commands. This does not grant that page new access.
            let url = payload.url();
            let title = if url.query_pairs().any(|(key, value)| {
                key == "recorded" && matches!(value.as_ref(), "stage2" | "stage3")
            }) {
                "Research Atlas · Recorded offline test"
            } else if url.scheme() == "http"
                && url.host_str() == Some("127.0.0.1")
                && url.path() == "/live"
            {
                "Research Atlas · Live local research"
            } else {
                "Research Atlas"
            };
            let _ = webview.window().set_title(title);
        })
        .invoke_handler(tauri::generate_handler![
            open_live_workspace,
            connect_live_workspace,
            get_live_workspace_selection,
            set_atlas_view_title,
            load_live_draft,
            save_live_draft
        ])
        .setup(|app| {
            use tauri::menu::{Menu, MenuItem, MenuItemKind, Submenu};

            let menu = Menu::default(app.handle())?;
            let reconnect = MenuItem::with_id(
                app.handle(),
                RECONNECT_MENU_ID,
                "Reconnect saved workspace",
                true,
                None::<&str>,
            )?;
            if let Some(file) = menu.items()?.into_iter().find_map(|item| match item {
                MenuItemKind::Submenu(submenu)
                    if submenu.text().ok().as_deref() == Some("File") =>
                {
                    Some(submenu)
                }
                _ => None,
            }) {
                file.prepend(&reconnect)?;
            } else {
                let file = Submenu::with_items(app.handle(), "File", true, &[&reconnect])?;
                menu.prepend(&file)?;
            }
            app.set_menu(menu)?;
            if let Ok(value) = std::env::var("ORA_LIVE_URL") {
                let url = live_url(&value)?;
                if let Some(window) = app.get_webview_window("main") {
                    let selected = std::env::var("ORA_LIVE_STATE_DIR")
                        .ok()
                        .and_then(|path| fs::canonicalize(path).ok());
                    let access = selected.as_ref().and_then(|directory| {
                        let verified = validated_connection(directory).ok()?;
                        if verified != url {
                            return None;
                        }
                        authenticated_draft_access(directory, &verified)
                    });
                    set_draft_access(&window, None);
                    if let Some(directory) = selected {
                        if save_state_dir(&window, &directory).is_ok() {
                            set_draft_access(&window, access);
                        }
                    }
                    window.navigate(url)?;
                }
            } else if let Some(window) = app.get_webview_window("main") {
                if let Some(directory) = selected_state_dir(&window) {
                    let _ = navigate_live(&window, &directory, "history", None);
                }
            }
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("failed to start the sample desktop prototype");
}

#[cfg(test)]
mod tests {
    use super::{allows_draft_access, draft_access_from_connection, selected_run_from_url};
    use std::path::Path;

    #[test]
    fn remote_capability_grants_only_draft_commands_to_local_live_page() {
        let remote: serde_json::Value =
            serde_json::from_str(include_str!("../capabilities/live-drafts.json")).unwrap();
        assert_eq!(remote["local"], false);
        assert_eq!(remote["windows"], serde_json::json!(["main"]));
        assert_eq!(
            remote["remote"]["urls"],
            serde_json::json!(["http://127.0.0.1:*/live"])
        );
        assert_eq!(
            remote["permissions"],
            serde_json::json!(["allow-load-live-draft", "allow-save-live-draft"])
        );
        let pattern: tauri::utils::acl::RemoteUrlPattern = remote["remote"]["urls"][0]
            .as_str()
            .unwrap()
            .parse()
            .unwrap();
        assert!(
            pattern.test(&tauri::Url::parse("http://127.0.0.1:51986/live?view=request").unwrap())
        );
        assert!(
            pattern.test(&tauri::Url::parse("http://127.0.0.1:63210/live?view=history").unwrap())
        );
        for url in [
            "http://127.0.0.1:51986/?recorded=stage3",
            "http://127.0.0.1:51986/other",
            "http://localhost:51986/live",
            "https://127.0.0.1:51986/live",
        ] {
            assert!(!pattern.test(&tauri::Url::parse(url).unwrap()));
        }

        let local: serde_json::Value =
            serde_json::from_str(include_str!("../capabilities/default.json")).unwrap();
        let permissions = local["permissions"].as_array().unwrap();
        for name in [
            "allow-open-live-workspace",
            "allow-connect-live-workspace",
            "allow-get-live-workspace-selection",
            "allow-set-atlas-view-title",
            "allow-load-live-draft",
            "allow-save-live-draft",
        ] {
            assert!(permissions.contains(&serde_json::json!(name)));
        }
    }

    #[test]
    fn draft_access_requires_verified_connection_identity_and_token() {
        let directory = Path::new("/synthetic/research");
        let verified =
            tauri::Url::parse("http://127.0.0.1:51986/live#token=synthetic-token").unwrap();
        let connection = serde_json::json!({
            "origin": "http://127.0.0.1:51986",
            "token": "synthetic-token",
            "launchUrl": verified.as_str(),
            "workspaceId": "synthetic-workspace",
        });
        let access = draft_access_from_connection(directory, &verified, &connection.to_string())
            .expect("verified selected host authorizes its draft");
        assert_eq!(access.workspace_id, "synthetic-workspace");
        assert_eq!(access.state_dir, directory);

        for changed in [
            serde_json::json!({"origin":"http://127.0.0.1:51986","token":"other-token","launchUrl":verified.as_str(),"workspaceId":"synthetic-workspace"}),
            serde_json::json!({"origin":"http://127.0.0.1:51986","token":"synthetic-token","launchUrl":"http://127.0.0.1:51986/live#token=other-token","workspaceId":"synthetic-workspace"}),
            serde_json::json!({"origin":"http://127.0.0.1:51987","token":"synthetic-token","launchUrl":verified.as_str(),"workspaceId":"synthetic-workspace"}),
            serde_json::json!({"origin":"http://127.0.0.1:51986","token":"synthetic-token","launchUrl":verified.as_str(),"workspaceId":"../other"}),
        ] {
            assert!(
                draft_access_from_connection(directory, &verified, &changed.to_string()).is_none()
            );
        }
        assert!(draft_access_from_connection(directory, &verified, "not json").is_none());
    }

    #[test]
    fn draft_access_rejects_recorded_foreign_and_other_workspace_pages() {
        let directory = Path::new("/synthetic/research");
        let verified =
            tauri::Url::parse("http://127.0.0.1:51986/live#token=synthetic-token").unwrap();
        let connection = serde_json::json!({
            "origin": "http://127.0.0.1:51986",
            "token": "synthetic-token",
            "launchUrl": verified.as_str(),
            "workspaceId": "synthetic-workspace",
        });
        let access =
            draft_access_from_connection(directory, &verified, &connection.to_string()).unwrap();
        let current = tauri::Url::parse("http://127.0.0.1:51986/live?view=request").unwrap();
        assert!(allows_draft_access(
            &access,
            &current,
            directory,
            "synthetic-workspace"
        ));
        assert!(!allows_draft_access(
            &access,
            &current,
            directory,
            "other-workspace"
        ));
        assert!(!allows_draft_access(
            &access,
            &current,
            Path::new("/synthetic/other"),
            "synthetic-workspace"
        ));
        for url in [
            "http://127.0.0.1:51987/live?view=request",
            "http://localhost:51986/live?view=request",
            "http://127.0.0.1:51986/?recorded=stage3",
            "http://127.0.0.1:51986/live-other",
            "https://127.0.0.1:51986/live",
        ] {
            assert!(!allows_draft_access(
                &access,
                &tauri::Url::parse(url).unwrap(),
                directory,
                "synthetic-workspace"
            ));
        }
    }

    #[test]
    fn carries_one_safe_run_from_a_local_live_page() {
        let url = tauri::Url::parse(
            "http://127.0.0.1:64348/live?view=overview&runId=run-abc_123#token=private",
        )
        .unwrap();
        assert_eq!(selected_run_from_url(&url).as_deref(), Some("run-abc_123"));
    }

    #[test]
    fn ignores_foreign_ambiguous_or_invalid_run_selection() {
        for value in [
            "http://example.test/live?runId=run-1",
            "http://127.0.0.1:64348/other?runId=run-1",
            "http://127.0.0.1:64348/live?runId=run-1&runId=run-2",
            "http://127.0.0.1:64348/live?runId=unsafe%2Fpath",
            "http://127.0.0.1:64348/live?runId=",
        ] {
            assert_eq!(
                selected_run_from_url(&tauri::Url::parse(value).unwrap()),
                None
            );
        }
    }
}
