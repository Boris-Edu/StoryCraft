// Copyright (c) 2026 Szymon Wolarz
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
// Modified for StoryCraft.

//! StoryCraft setup engine.
//! The install logic lives in one place, setupfiles/setup.py (the same script the terminal installer
//! runs). This engine only prepares a portable Python, runs setup.py without prompts and turns its
//! output into the progress, log and completion events the window shows.

use crate::os;
use crate::state::{emit_complete, emit_log, emit_progress_sub, EventSender};
use std::io::{BufRead, BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::process::Stdio;

pub const APP_VERSION: &str = env!("CARGO_PKG_VERSION");

const REPO: &str = "Boris-Edu/StoryCraft";
const BRANCH: &str = "fork";
/// Copy of setup.py from build time, used when the current one can't be downloaded
const SETUP_PY_EMBEDDED: &str = include_str!("../../setupfiles/setup.py");
/// Same portable Python as the terminal bootstrap scripts (python-build-standalone)
const PBS_RELEASES_API: &str = "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest";
const PBS_FALLBACK_TAG: &str = "20250317";
const PBS_FALLBACK_VER: &str = "3.12.9";
const UA: &str = "StoryCraft-Setup";

// ── Portable Python ─────────────────────────────────────────────────────────

/// <cache>/StoryCraft-bootstrap, shared with the terminal bootstrap scripts
fn bootstrap_dir() -> PathBuf {
    dirs::cache_dir()
        .unwrap_or_else(std::env::temp_dir)
        .join("StoryCraft-bootstrap")
}

fn python_dir() -> PathBuf {
    // On Windows the PowerShell bootstrap keeps an embeddable Python in "python": stay out of its way
    if cfg!(target_os = "windows") {
        bootstrap_dir().join("python-pbs")
    } else {
        bootstrap_dir().join("python")
    }
}

fn pbs_triple() -> Option<&'static str> {
    if cfg!(all(target_os = "macos", target_arch = "aarch64")) {
        Some("aarch64-apple-darwin")
    } else if cfg!(all(target_os = "macos", target_arch = "x86_64")) {
        Some("x86_64-apple-darwin")
    } else if cfg!(all(target_os = "linux", target_arch = "x86_64")) {
        Some("x86_64-unknown-linux-gnu")
    } else if cfg!(all(target_os = "linux", target_arch = "aarch64")) {
        Some("aarch64-unknown-linux-gnu")
    } else if cfg!(all(target_os = "windows", target_arch = "x86_64")) {
        Some("x86_64-pc-windows-msvc")
    } else {
        None
    }
}

fn platform_arg() -> &'static str {
    if cfg!(target_os = "windows") {
        "windows"
    } else if cfg!(target_os = "macos") {
        "macos"
    } else {
        "linux"
    }
}

fn python_exe(dir: &Path) -> PathBuf {
    if cfg!(target_os = "windows") {
        dir.join("python.exe")
    } else {
        dir.join("bin").join("python3")
    }
}

/// Python 3.10+ with pip
fn python_works(py: &Path) -> bool {
    let version_ok = os::create_hidden_command(py)
        .args(["-c", "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|s| s.success())
        .unwrap_or(false);
    version_ok
        && os::create_hidden_command(py)
            .args(["-m", "pip", "--version"])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .map(|s| s.success())
            .unwrap_or(false)
}

fn pbs_download_url(triple: &str) -> String {
    let suffix = format!("{}-install_only.tar.gz", triple);
    if let Ok(resp) = ureq::get(PBS_RELEASES_API).set("User-Agent", UA).call() {
        if let Ok(json) = resp.into_json::<serde_json::Value>() {
            if let Some(assets) = json["assets"].as_array() {
                for a in assets {
                    let name = a["name"].as_str().unwrap_or("");
                    if name.starts_with("cpython-3.12") && name.ends_with(&suffix) {
                        if let Some(u) = a["browser_download_url"].as_str() {
                            return u.to_string();
                        }
                    }
                }
            }
        }
    }
    format!(
        "https://github.com/astral-sh/python-build-standalone/releases/download/{tag}/cpython-{ver}+{tag}-{suffix}",
        tag = PBS_FALLBACK_TAG,
        ver = PBS_FALLBACK_VER,
        suffix = suffix
    )
}

/// Downloads `url` to `dest`, reporting progress between `from` and `to` percent
fn download(url: &str, dest: &Path, sender: &EventSender, from: u32, to: u32, status: &str) -> bool {
    let resp = match ureq::get(url).set("User-Agent", UA).call() {
        Ok(r) => r,
        Err(e) => {
            emit_log(sender, "ERROR", &format!("Download failed: {} ({})", url, e));
            return false;
        }
    };
    let total: u64 = resp.header("Content-Length").and_then(|v| v.parse().ok()).unwrap_or(0);
    let mut reader = resp.into_reader();
    let mut file = match std::fs::File::create(dest) {
        Ok(f) => f,
        Err(e) => {
            emit_log(sender, "ERROR", &format!("Cannot write {}: {}", dest.display(), e));
            return false;
        }
    };
    let mut buf = vec![0u8; 256 * 1024];
    let mut done: u64 = 0;
    let mut last_sub = u32::MAX;
    loop {
        let n = match reader.read(&mut buf) {
            Ok(0) => break,
            Ok(n) => n,
            Err(e) => {
                emit_log(sender, "ERROR", &format!("Download interrupted: {}", e));
                return false;
            }
        };
        if file.write_all(&buf[..n]).is_err() {
            return false;
        }
        done += n as u64;
        if total > 0 {
            let sub = ((done * 100) / total) as u32;
            if sub != last_sub {
                last_sub = sub;
                let pct = from + (to - from) * sub / 100;
                emit_progress_sub(sender, pct, sub, status, &format!("{} / {} MB", done / 1_048_576, total / 1_048_576));
            }
        }
    }
    true
}

/// A portable Python for running setup.py (cached; downloaded once, ~25 MB)
fn ensure_python(sender: &EventSender) -> Option<PathBuf> {
    let dir = python_dir();
    let py = python_exe(&dir);
    if py.is_file() && python_works(&py) {
        emit_log(sender, "INFO", &format!("Using portable Python: {}", py.display()));
        return Some(py);
    }
    let triple = match pbs_triple() {
        Some(t) => t,
        None => {
            emit_log(sender, "ERROR", "This computer's system or processor isn't supported by the setup app.");
            return None;
        }
    };
    emit_progress_sub(sender, 1, 0, "Preparing portable Python", "python-build-standalone");
    let url = pbs_download_url(triple);
    emit_log(sender, "INFO", &format!("Downloading portable Python: {}", url));
    let _ = std::fs::remove_dir_all(&dir);
    if std::fs::create_dir_all(&dir).is_err() {
        emit_log(sender, "ERROR", &format!("Cannot create {}", dir.display()));
        return None;
    }
    let archive = bootstrap_dir().join("python.tar.gz");
    if !download(&url, &archive, sender, 1, 4, "Downloading portable Python") {
        return None;
    }
    // tar is built into macOS, Linux and Windows 10+
    let ok = os::create_hidden_command("tar")
        .arg("-xzf")
        .arg(&archive)
        .arg("-C")
        .arg(&dir)
        .arg("--strip-components=1")
        .status()
        .map(|s| s.success())
        .unwrap_or(false);
    let _ = std::fs::remove_file(&archive);
    if !ok || !py.is_file() {
        emit_log(sender, "ERROR", "Could not unpack the portable Python.");
        return None;
    }
    if !python_works(&py) {
        let _ = os::create_hidden_command(&py).args(["-m", "ensurepip", "--upgrade"]).status();
    }
    if python_works(&py) {
        Some(py)
    } else {
        emit_log(sender, "ERROR", "The portable Python doesn't run on this computer.");
        None
    }
}

/// setup.py's one dependency (rich), kept beside the Python as the terminal bootstrap does
fn ensure_rich(py: &Path, sender: &EventSender) -> PathBuf {
    let pkg = bootstrap_dir().join("packages");
    if !pkg.join("rich").is_dir() {
        emit_progress_sub(sender, 4, 0, "Preparing the installer", "rich");
        let _ = os::create_hidden_command(py)
            .args(["-m", "pip", "install", "rich", "--quiet", "--target"])
            .arg(&pkg)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
    }
    pkg
}

/// The current setup.py from the repository, or the copy built into this app
fn setup_script(sender: &EventSender) -> Option<PathBuf> {
    let path = bootstrap_dir().join("setup.py");
    // STORYCRAFT_GH_RAW replaces raw.githubusercontent.com, as in the bootstrap scripts (tests, mirrors)
    let raw = std::env::var("STORYCRAFT_GH_RAW").unwrap_or_else(|_| "https://raw.githubusercontent.com".to_string());
    let url = format!("{}/{}/{}/setupfiles/setup.py", raw.trim_end_matches('/'), REPO, BRANCH);
    let fresh = ureq::get(&url)
        .set("User-Agent", UA)
        .call()
        .ok()
        .and_then(|r| r.into_string().ok())
        .filter(|s| s.contains("def option_install_update"));
    let text = match fresh {
        Some(s) => s,
        None => {
            emit_log(sender, "WARN", "Could not download the current setup script; using the one built into this app.");
            SETUP_PY_EMBEDDED.to_string()
        }
    };
    match std::fs::write(&path, text) {
        Ok(_) => Some(path),
        Err(e) => {
            emit_log(sender, "ERROR", &format!("Cannot write {}: {}", path.display(), e));
            None
        }
    }
}

// ── Running setup.py ────────────────────────────────────────────────────────

/// Removes terminal colour codes and title sequences
fn strip_ansi(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut chars = s.chars().peekable();
    while let Some(c) = chars.next() {
        if c == '\u{1b}' {
            match chars.peek() {
                Some('[') => {
                    chars.next();
                    while let Some(&n) = chars.peek() {
                        chars.next();
                        if n.is_ascii_alphabetic() {
                            break;
                        }
                    }
                }
                Some(']') => {
                    chars.next();
                    while let Some(n) = chars.next() {
                        if n == '\u{7}' {
                            break;
                        }
                    }
                }
                _ => {}
            }
        } else {
            out.push(c);
        }
    }
    out
}

/// Where each setup.py step sits on the overall bar (install / update)
const STEP_PROGRESS: &[(&str, u32)] = &[
    ("Resolving source files", 6),
    ("Downloading release", 8),
    ("Extracting release archive", 16),
    ("Detecting GPU", 18),
    ("Preparing directories", 20),
    ("Deploying fresh application files", 22),
    ("FFmpeg", 26),
    ("Creating virtual environment", 34),
    ("Installing / upgrading dependencies", 38),
    ("Installing Faster-Whisper", 40),
    ("Installing PySide6", 70),
    ("Verifying installed packages", 88),
    ("libs", 91),
    ("DaVinci Resolve integration", 94),
    ("shortcut", 97),
];

struct StepTracker {
    percent: u32,
    status: String,
}

impl StepTracker {
    fn update(&mut self, line: &str, sender: &EventSender) {
        let t = line.trim();
        // setup.py marks steps "[....] Doing x...", "[ OK ] x", "[|]  x" (spinner), "[WARN] x"
        let (tag, text) = match (t.find('['), t.find(']')) {
            (Some(0), Some(e)) if e <= 6 => (&t[1..e], t[e + 1..].trim()),
            _ => ("", t),
        };
        if text.is_empty() {
            return;
        }
        let is_step = matches!(tag, "...." | "|" | "/" | "-" | "\\");
        if is_step {
            let target = STEP_PROGRESS
                .iter()
                .find(|(k, _)| text.contains(k))
                .map(|(_, p)| *p)
                // unknown step (repair, reset, uninstall): creep forward, never reaching the end
                .unwrap_or(self.percent + (96u32.saturating_sub(self.percent)) / 8);
            self.percent = self.percent.max(target).min(97);
            self.status = text.trim_end_matches("...").to_string();
        }
        let pct = self.percent;
        emit_progress_sub(sender, pct, if is_step { 50 } else { 100 }, &self.status, text);
    }
}

/// Runs setup.py with `args` on a worker thread (the window calls this from its UI thread)
fn run_setup(action: &'static str, args: Vec<String>, sender: EventSender) {
    run_setup_with_env(action, args, Vec::new(), sender);
}

fn run_setup_with_env(action: &'static str, args: Vec<String>, env: Vec<(&'static str, String)>, sender: EventSender) {
    std::thread::spawn(move || run_setup_blocking(action, args, env, sender));
}

/// Runs setup.py with `args` (plus the platform and Python it needs), streaming its output
fn run_setup_blocking(action: &str, args: Vec<String>, env: Vec<(&str, String)>, sender: EventSender) {
    let py = match ensure_python(&sender) {
        Some(p) => p,
        None => return emit_complete(&sender, action, false, "Could not prepare Python for the installer. See the log for details."),
    };
    let pkg = ensure_rich(&py, &sender);
    let script = match setup_script(&sender) {
        Some(s) => s,
        None => return emit_complete(&sender, action, false, "Could not prepare the setup script."),
    };

    let mut cmd = os::create_hidden_command(&py);
    cmd.arg("-u")
        .arg(&script)
        .args(["--platform", platform_arg(), "--bootstrap-python"])
        .arg(&py)
        .args(&args)
        .args(["--non-interactive", "--no-launch"])
        .env("PYTHONPATH", &pkg)
        .env("PYTHONUNBUFFERED", "1")
        .env("PYTHONIOENCODING", "utf-8")
        .env("TERM", "dumb")
        .env("NO_COLOR", "1")
        .envs(env)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    emit_log(&sender, "INFO", &format!("Running setup.py {}", args.join(" ")));

    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => return emit_complete(&sender, action, false, &format!("Could not start the installer: {}", e)),
    };

    // stderr goes to the log as it comes (tracebacks, tar and pip errors)
    let err_sender = sender.clone();
    let stderr = child.stderr.take();
    let err_thread = std::thread::spawn(move || {
        if let Some(e) = stderr {
            for line in BufReader::new(e).lines().map_while(Result::ok) {
                let clean = strip_ansi(&line);
                if !clean.trim().is_empty() {
                    emit_log(&err_sender, "ERROR", clean.trim_end());
                }
            }
        }
    });

    let mut tracker = StepTracker { percent: 5, status: "Starting".to_string() };
    let mut last_problem = String::new();
    if let Some(out) = child.stdout.take() {
        for raw in BufReader::new(out).lines().map_while(Result::ok) {
            // spinners redraw with \r: keep the latest state of the line
            let clean = strip_ansi(&raw);
            let line = clean.rsplit('\r').find(|s| !s.trim().is_empty()).unwrap_or("").trim_end();
            if line.trim().is_empty() {
                continue;
            }
            let level = if line.contains("[ERR!]") {
                last_problem = line.trim().to_string();
                "ERROR"
            } else if line.contains("[WARN]") {
                "WARN"
            } else if line.contains("[ OK ]") {
                "SUCCESS"
            } else {
                "INFO"
            };
            emit_log(&sender, level, line.trim());
            tracker.update(line, &sender);
        }
    }
    let _ = err_thread.join();
    let ok = child.wait().map(|s| s.success()).unwrap_or(false);
    if ok {
        emit_progress_sub(&sender, 100, 100, "Done", "");
        emit_complete(&sender, action, true, "");
    } else {
        let msg = if last_problem.is_empty() { "The installer stopped with an error. See the log for details.".to_string() } else { last_problem };
        emit_complete(&sender, action, false, &msg);
    }
}

fn path_arg(p: &Path) -> String {
    p.to_string_lossy().into_owned()
}

// ── Actions the window calls ────────────────────────────────────────────────

/// Install or update StoryCraft (setup.py makes the shortcuts the checkboxes ask for and the Resolve menu entry)
pub fn run_install(target_dir: PathBuf, create_desktop: bool, create_menu: bool, sender: EventSender) {
    let flag = |b: bool| if b { "1".to_string() } else { "0".to_string() };
    let env = vec![("STORYCRAFT_DESKTOP_SHORTCUT", flag(create_desktop)), ("STORYCRAFT_MENU_SHORTCUT", flag(create_menu))];
    run_setup_with_env("install", vec!["--install".into(), path_arg(&target_dir)], env, sender);
}

/// Branch installs are for development: the setup app always installs the latest release
pub fn run_dev_install(target_dir: PathBuf, create_desktop: bool, create_menu: bool, _branch: Option<String>, sender: EventSender) {
    run_install(target_dir, create_desktop, create_menu, sender);
}

pub fn run_repair(target_dir: PathBuf, sender: EventSender) {
    run_setup("repair", vec!["--repair".into(), "--install".into(), path_arg(&target_dir)], sender);
}

pub fn run_reset(target_dir: PathBuf, sender: EventSender) {
    run_setup("reset", vec!["--reset".into(), "--install".into(), path_arg(&target_dir)], sender);
}

/// Uninstall keeps the user's saved transcripts and stories (setup.py's rule)
pub fn run_uninstall(target_dir: PathBuf, _uninstall_system_python: bool, sender: EventSender) {
    run_setup("uninstall", vec!["--uninstall".into(), "--install".into(), path_arg(&target_dir)], sender);
}

/// Moving needs setup.py's interactive mode: not offered in the window (the menu hides it)
pub fn run_move(_from_dir: PathBuf, _to_dir: PathBuf, sender: EventSender) {
    emit_complete(&sender, "move", false, "Moving an installation isn't available in the setup app yet. Use the terminal installer.");
}

// ── Lookups for the welcome screen ──────────────────────────────────────────

/// The setup app always installs a GitHub release, never local files
pub fn detect_local_version() -> Option<String> {
    None
}

pub fn find_local_repo() -> Option<PathBuf> {
    None
}

pub fn fetch_git_branches() -> Vec<String> {
    Vec::new()
}

/// (tag, source zip) of the latest StoryCraft release
pub fn fetch_latest_github_release() -> Option<(String, String)> {
    let resp = ureq::get(&format!("https://api.github.com/repos/{}/releases/latest", REPO))
        .set("User-Agent", UA)
        .call()
        .ok()?;
    let json: serde_json::Value = resp.into_json().ok()?;
    let tag = json["tag_name"].as_str()?.to_string();
    let zip = format!("https://github.com/{}/archive/refs/tags/{}.zip", REPO, tag);
    Some((tag, zip))
}
