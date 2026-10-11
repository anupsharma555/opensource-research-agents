//! Prepare the native sample app for a Tauri build.
//! Cargo runs this file to generate Tauri's build metadata before compiling the
//! desktop shell. It contains no research or provider logic.
fn main() {
    let attributes =
        tauri_build::Attributes::new().app_manifest(tauri_build::AppManifest::new().commands(&[
            "open_live_workspace",
            "connect_live_workspace",
            "get_live_workspace_selection",
            "set_atlas_view_title",
            "load_live_draft",
            "save_live_draft",
        ]));
    tauri_build::try_build(attributes).expect("failed to prepare the Atlas native permissions");
}
