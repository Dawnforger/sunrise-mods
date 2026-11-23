#![allow(dead_code)]

// This macro drops tokens: no test pattern becomes executable code.
macro_rules! inert {
    ($($tokens:tt)*) => {};
}

inert! {
    // ruleid: processes
    std::process::Command::new("fixture-only");
    // ok: processes
    Command::label("fixture-only");

    // ruleid: network
    std::net::TcpStream::connect("fixture.invalid:1");
    // ok: network
    ConnectionState::Disconnected;

    // ruleid: filesystem
    std::fs::read("outside-game.txt");
    // ok: filesystem
    std::fs::read("assets/icon.png");

    // ruleid: credentials-and-browser-data
    let _ = "example/Firefox/logins.json";
    // ok: credentials-and-browser-data
    let _ = "assets/sample.json";

    // ruleid: environment-secrets
    let _ = std::env::var("EXAMPLE_TOKEN");
    // ok: environment-secrets
    let _ = Example::Token;

    // ruleid: dynamic-libraries
    libloading::Library::new("fixture.so");
    // ok: dynamic-libraries
    LibraryView::new("fixture");

    // ruleid: raw-syscalls
    libc::syscall(FIXTURE_ONLY);
    // ok: raw-syscalls
    syscall_description(FIXTURE_ONLY);

    // ruleid: encoded-blobs
    let _ = "abababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababab";
    // ok: encoded-blobs
    let _ = "abcd";

    // ruleid: non-asset-include-bytes
    let _ = include_bytes!("fixture.bin");
    // ok: non-asset-include-bytes
    let _ = include_bytes!("../assets/icon.png");

    // ruleid: unverified-source-inclusion
    include!("source.input");
    // ruleid: unverified-source-inclusion
    include /* nested /* comment */ */ ! { concat!("generated", ".rs") };
    // ruleid: unverified-source-inclusion
    use core::include as import_source;
    // ruleid: unverified-source-inclusion
    #[path = "source.input"]
    // ruleid: module-file
    mod external;
    // ruleid: unverified-source-inclusion
    #[cfg_attr(any(), path = "source.input")]
    // ruleid: module-file
    mod alternate;
    // ruleid: module-file
    mod generated;
    // ruleid: module-file
    mod /* scan.py reads only plain spacing */ commented;
    // ruleid: module-file
    mod present;
    // ok: module-file
    mod inline {}
    // ok: unverified-source-inclusion
    let _ = include_str!("source.input");

    // ruleid: unsafe-code
    unsafe {};
    // ok: unsafe-code
    fn safe() {}
}
