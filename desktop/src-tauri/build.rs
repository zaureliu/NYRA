fn main() {
    #[cfg(target_os = "windows")]
    {
        cc::Build::new()
            .cpp(true)
            .file("src/spout_presence.cpp")
            .flag_if_supported("/std:c++17")
            .flag_if_supported("/EHsc")
            .compile("kazumi_spout_presence");
        for library in ["d3d11", "dxgi", "d2d1", "dcomp", "ole32"] {
            println!("cargo:rustc-link-lib={library}");
        }
        println!("cargo:rerun-if-changed=src/spout_presence.cpp");
        println!("cargo:rerun-if-changed=src/spout_presence.hpp");
    }
    tauri_build::build()
}
