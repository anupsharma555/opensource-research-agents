//! Open the bundled sample or an explicitly launched local research connection.
//! The Python host owns research execution and credentials. Closing this window
//! leaves that separate process and its saved work running.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use tauri::Manager;

fn live_url(value: &str) -> Result<tauri::Url, String> {
    let url = tauri::Url::parse(value).map_err(|_| "Invalid local research connection.")?;
    if url.scheme() != "http"
        || url.host_str() != Some("127.0.0.1")
        || url.port().is_none()
        || url.path() != "/live"
        || !url.username().is_empty()
        || url.password().is_some()
    {
        return Err("Live research must use the explicit local host".into());
    }
    Ok(url)
}

#[tauri::command]
fn open_live_workspace(window: tauri::WebviewWindow, view: &str) -> Result<(), String> {
    if !matches!(view, "request" | "history") {
        return Err("Unknown live workspace view.".into());
    }
    let value = std::env::var("ORA_LIVE_URL").map_err(|_| {
        "This window was opened without a live connection. Reopen it with the local live launcher using your saved state directory."
    })?;
    let mut url = live_url(&value)?;
    url.set_query(Some(&format!("view={view}")));
    window.set_title("Research Atlas · Live local research").map_err(|_| "Could not update the window title.")?;
    window.navigate(url).map_err(|_| "Could not open the local live workspace.".into())
}

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![open_live_workspace])
        .setup(|app| {
            if let Ok(value) = std::env::var("ORA_LIVE_URL") {
                let url = live_url(&value)?;
                if let Some(window) = app.get_webview_window("main") {
                    window.set_title("Research Atlas · Live local research")?;
                    window.navigate(url)?;
                }
            }
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("failed to start the sample desktop prototype");
}
