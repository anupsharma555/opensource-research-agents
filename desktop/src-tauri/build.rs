//! Prepare the native sample app for a Tauri build.
//! Cargo runs this file to generate Tauri's build metadata before compiling the
//! desktop shell. It contains no research or provider logic.
fn main() {
    tauri_build::build()
}
