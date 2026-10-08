//! Launch the Mac window that displays the bundled React research sample.
//! Tauri supplies the native shell around the saved review interface. This file
//! does not start a research host or contact a model or provider.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    tauri::Builder::default()
        .run(tauri::generate_context!())
        .expect("failed to start the sample desktop prototype");
}
