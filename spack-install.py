#!/usr/bin/env python3
"""
GNU Make Jobserver Client Example

This script demonstrates how to implement a client for the GNU Make jobserver protocol
using Python's select-based event loop. The jobserver protocol allows multiple build
processes to coordinate and share a limited pool of job tokens to control parallelism.

Key Features:
- Implements the GNU Make jobserver FIFO protocol for job token management
- Uses select() for non-blocking I/O multiplexing across multiple child processes
- Provides two display modes: package overview (default) and verbose log output
- Handles signal-based child process cleanup with proper resource management
- Simulates parallel package builds with realistic build stages (configure/build/install)

Usage:
- Run standalone: Creates its own jobserver with specified number of jobs (-j flag)
- Run under Make: Automatically detects and uses existing jobserver from MAKEFLAGS
- Press 'v' during execution to toggle between overview and verbose log modes

Architecture:
The event loop monitors multiple file descriptors simultaneously:
- Jobserver FIFO: For acquiring/releasing job tokens
- Child stdout/stderr: For capturing build output and state changes
- Signal pipe: For SIGCHLD notifications when processes terminate
- stdin: For user input (mode toggling)

This example serves as a reference implementation for integrating custom build tools
with GNU Make's parallel job control system.
"""

import argparse
import json
import multiprocessing
import multiprocessing.connection
import os
import re
import selectors
import signal
import subprocess
import sys
import tempfile
import termios
import time
import tty
from typing import Dict, List, Optional, Tuple

#: How often to update a spinner in seconds
SPINNER_INTERVAL = 0.1

#: How long to display finished packages before graying them out
CLEANUP_TIMEOUT = 2.0

#: Size of the output buffer for child processes
OUTPUT_BUFFER_SIZE = 4096


class BuildArgs:
    """Arguments for a job to be executed."""

    def __init__(
        self,
        package_name: str,
        version: str,
        hash_str: str,
        explicit: bool = False,
        should_fail: bool = False,
    ) -> None:
        self.package_name = package_name
        self.version = version
        self.hash_str = hash_str
        self.explicit = explicit  # True if explicitly requested, False if dependency
        self.should_fail = should_fail  # True if this package should fail to build


class BuildInfo:
    """Information about a package being built."""

    def __init__(self, version: str, hash_str: str, explicit: bool) -> None:
        self.state: str = "starting"
        self.explicit: bool = explicit
        self.version: str = version
        self.hash_str: str = hash_str
        self.finished_time: Optional[float] = None
        self.grayed_time: Optional[float] = None
        self.progress_percent: Optional[int] = None


class BuildStatus:
    """Tracks the build status display for terminal output."""

    def __init__(self) -> None:
        #: Ordered dict of build ID -> info
        self.builds: Dict[str, BuildInfo] = {}
        self.spinner_chars = ["|", "/", "-", "\\"]
        self.spinner_index = 0
        self.dirty = True  # Start dirty to draw initial state
        self.last_lines_drawn = 0
        self.next_spinner_update = time.monotonic()
        self.spinner_interval = SPINNER_INTERVAL
        self.overview_mode = True  # Whether to draw the package overview
        self.tracked_build_id = ""  # identifier of the package whose logs we follow
        self.is_tty = sys.stdout.isatty()  # Whether stdout is a terminal

        # Frame dumping setup
        self.frame_counter = 0
        self.frames_dir = "/tmp/frames"
        os.makedirs(self.frames_dir, exist_ok=True)

    def add_build(
        self,
        build_id: str,
        version: str = "",
        hash_str: str = "",
        explicit: bool = False,
    ) -> None:
        """Add a new build to the display and mark the display as dirty."""
        if build_id not in self.builds:
            self.builds[build_id] = BuildInfo(version, hash_str, explicit)
            self.dirty = True

    def follow_logs(self, build_id: Optional[str]) -> None:
        """Follow logs of a specific build by its index in the builds dict."""
        if build_id is None:
            return
        self.tracked_build_id = build_id
        if self.overview_mode:
            self.toggle()
        print(f"Following logs for {build_id}", flush=True)

    def update_state(self, build_id: str, state: str) -> None:
        """Update the state of a package and mark the display as dirty."""
        pkg_info = self.builds[build_id]
        pkg_info.state = state
        pkg_info.progress_percent = None

        if state in ("finished", "failed"):
            pkg_info.finished_time = time.monotonic()

            # Return to overview mode when a build finishes
            if not self.overview_mode:
                self.overview_mode = True

        self.dirty = True

        # For non-TTY output, print state changes immediately without colors
        if not self.is_tty:
            print(f"{build_id}: {state}")

    def update_progress(self, package: str, current: int, total: int) -> None:
        """Update the progress of a package and mark the display as dirty."""
        percent = int((current / total) * 100)
        pkg_info = self.builds[package]
        if pkg_info.progress_percent != percent:
            pkg_info.progress_percent = percent
            self.dirty = True

    def tick(self, cleanup_timeout: float = CLEANUP_TIMEOUT) -> None:
        """Update spinner and clean up finished packages."""
        if not self.is_tty:
            return
        now = time.monotonic()

        # Only update the spinner if there are still running packages
        if now >= self.next_spinner_update and any(
            pkg.finished_time is None for pkg in self.builds.values()
        ):
            self.spinner_index = (self.spinner_index + 1) % len(self.spinner_chars)
            self.dirty = True
            self.next_spinner_update = now + self.spinner_interval

        packages_to_remove = []

        for package, pkg_info in self.builds.items():
            if pkg_info.explicit or pkg_info.state == "failed" or pkg_info.finished_time is None:
                continue

            if pkg_info.grayed_time is None and now - pkg_info.finished_time >= cleanup_timeout:
                self.builds[package].grayed_time = now

            # cleanup_timeout can be 0, so no elif.
            if pkg_info.grayed_time is not None and now - pkg_info.grayed_time >= cleanup_timeout:
                packages_to_remove.append(package)

        if packages_to_remove:
            for package in packages_to_remove:
                del self.builds[package]
            self.dirty = True

    def toggle(self) -> None:
        """Toggle the status display."""
        self.overview_mode = not self.overview_mode
        if not self.overview_mode:
            self.clear()
        else:
            self.dirty = True

    def logs(self, build_id: str, data: bytes) -> None:
        if build_id != self.tracked_build_id:
            return
        sys.stdout.buffer.write(data)
        sys.stdout.flush()

    def clear(self) -> None:
        """Clear the current display without redrawing."""
        if self.last_lines_drawn == 0:
            return
        output_parts = self._clear_instructions(self.last_lines_drawn)
        print("".join(output_parts), end="")
        sys.stdout.flush()
        self.last_lines_drawn = 0

    def redraw(self) -> None:
        """Clear old display and redraw all status lines."""
        # Skip redraw for non-TTY output or when not in overview mode
        if not self.is_tty or not self.overview_mode or not self.dirty:
            return

        # Build the entire output as a single string to avoid flickering
        output_parts = self._clear_instructions(self.last_lines_drawn)

        # Display current active packages (if any)
        for package, pkg_info in self.builds.items():
            if pkg_info.state:
                line = self._format_package_line(package, pkg_info)
                output_parts.append(f"{line}\n")
            else:
                output_parts.append("\033[K\n")

        # Print everything at once to avoid flickering
        print("".join(output_parts), end="", flush=True)

        # Update the number of lines drawn for the next clear cycle
        self.last_lines_drawn = len(self.builds)
        self.dirty = False

    def _format_package_line(self, package: str, pkg_info: BuildInfo) -> str:
        """Format a line for a package with proper styling."""

        # grayed out line
        if pkg_info.grayed_time is not None:
            return f"\033[2;37m[+] {pkg_info.hash_str} {package}@{pkg_info.version}\033[0m"

        if pkg_info.state == "failed":
            indicator = "\033[31m[x]\033[0m"  # red X
            suffix = ""
        elif pkg_info.state == "finished":
            indicator = "\033[32m[+]\033[0m"  # green
            suffix = ""
        else:
            indicator = f"[{self.spinner_chars[self.spinner_index]}]"
            if pkg_info.progress_percent is not None:
                suffix = f": fetching: {pkg_info.progress_percent}%"
            else:
                suffix = f": {pkg_info.state}"

        hash_part = f"\033[0;90m{pkg_info.hash_str}\033[0m "
        package_with_version = f"{package}\033[0;36m@{pkg_info.version}\033[0m"

        # Apply styling based on package type
        if pkg_info.explicit:
            # Explicit package - bold white for package name, keep colors for hash and version
            package_text = (
                f"{hash_part}\033[1;37m{package}\033[0m"
                f"\033[0;36m@{pkg_info.version}\033[0m{suffix}"
            )
        else:
            # Normal implicit package - keep original colors
            package_text = f"{hash_part}{package_with_version}{suffix}"

        return f"\033[K{indicator} {package_text}"

    def _clear_instructions(self, lines_to_clear: int) -> List[str]:
        """Build ANSI escape sequences for cursor movement and line clearing."""
        parts = []
        if lines_to_clear > 0:
            # Move cursor up to start of display area
            parts.append(f"\033[{lines_to_clear}A")
            # Clear all old lines
            for _ in range(lines_to_clear):
                parts.append("\033[K\n")
            # Move cursor back to start position
            parts.append(f"\033[{lines_to_clear}A")
        return parts


class FdInfo:
    """Information about a file descriptor mapping."""

    def __init__(self, pid: int, name: str) -> None:
        self.pid = pid
        self.name = name


class ChildInfo:
    """Information about a child process."""

    def __init__(
        self,
        proc: multiprocessing.Process,
        package_name: str,
        output_r_conn: multiprocessing.connection.Connection,
        state_r_conn: multiprocessing.connection.Connection,
        explicit: bool = False,
    ) -> None:
        self.proc = proc
        self.package_name = package_name
        self.output_r_conn = output_r_conn
        self.state_r_conn = state_r_conn
        self.output_r = output_r_conn.fileno()
        self.state_r = state_r_conn.fileno()
        self.explicit = explicit


# Buffer for incoming, partially received state data from child processes
state_buffers: Dict[int, str] = {}


def worker_function(
    job_args: BuildArgs,
    output_w_conn: multiprocessing.connection.Connection,
    state_w_conn: multiprocessing.connection.Connection,
) -> None:
    """Worker function that redirects stdout/stderr to provided pipes."""
    # Get the raw file descriptors from the connection objects.
    output_w = output_w_conn.fileno()
    state_w = state_w_conn.fileno()

    # In the child process, re-open stdout to use the pipe with line-buffering.
    # Set closefd=False so fdopen doesn't close the fd owned by the Connection.
    # Redirect stderr to stdout.
    os.dup2(output_w, sys.stdout.fileno())
    os.dup2(output_w, sys.stderr.fileno())

    state_pipe = os.fdopen(state_w, "w", buffering=1, closefd=False)

    def send_state(state: str) -> None:
        """Send a state update message."""
        json.dump({"state": state}, state_pipe, separators=(",", ":"))
        state_pipe.write("\n")

    def send_progress(current: int, total: int) -> None:
        """Send a progress update message."""
        json.dump({"progress": current, "total": total}, state_pipe, separators=(",", ":"))
        state_pipe.write("\n")

    with tempfile.TemporaryDirectory() as build_dir:
        makefile_path = os.path.join(build_dir, "Makefile")
        with open("example.mk", "r") as src, open(makefile_path, "w") as dst:
            dst.write(src.read())
        os.chdir(build_dir)

        send_state("build")
        subprocess.run(["make"])

        send_state("install")
        subprocess.run(["make", "install"])

        if job_args.should_fail:
            raise Exception("Oh no!!")

        print("Installation completed successfully")

    # Explicitly close the connections when the worker is done.
    output_w_conn.close()
    state_w_conn.close()


def get_jobserver_fifo_path() -> Optional[str]:
    """Parse MAKEFLAGS for jobserver FIFO path from --jobserver-auth=fifo:<path>."""
    makeflags = os.environ.get("MAKEFLAGS", "")
    match = re.search(r"--jobserver-auth=fifo:([^ ]+)", makeflags)
    return match.group(1) if match else None


def create_jobserver_fifo(num_jobs: int) -> Tuple[int, int]:
    """Create a new jobserver FIFO with the specified number of job tokens."""
    tmpdir = tempfile.mkdtemp()
    fifo_path = os.path.join(tmpdir, "jobserver_fifo")

    try:
        os.mkfifo(fifo_path, 0o600)
        read_fd = os.open(fifo_path, os.O_RDONLY | os.O_NONBLOCK)
        write_fd = os.open(fifo_path, os.O_WRONLY)
        os.write(write_fd, b"+" * (num_jobs - 1))
        os.environ["MAKEFLAGS"] = f" -j{num_jobs} --jobserver-auth=fifo:{fifo_path}"
        return read_fd, write_fd

    except OSError as e:
        print(f"Error creating jobserver FIFO: {e}", file=sys.stderr)
        # Clean up on error
        if os.path.exists(fifo_path):
            os.unlink(fifo_path)
        if os.path.exists(tmpdir):
            os.rmdir(tmpdir)
        sys.exit(1)


def open_existing_jobserver_fifo(fifo_path: str) -> Tuple[int, int]:
    """Open an existing jobserver FIFO for reading and writing."""
    print(f"Jobserver FIFO found: {fifo_path}")
    try:
        read_fd = os.open(fifo_path, os.O_RDONLY | os.O_NONBLOCK)
        write_fd = os.open(fifo_path, os.O_WRONLY)
        return read_fd, write_fd
    except OSError as e:
        print(f"Error opening FIFO {fifo_path}: {e}", file=sys.stderr)
        sys.exit(1)


def setup_jobserver(num_jobs: int) -> Tuple[int, int]:
    """Set up the jobserver FIFO, either by finding an existing one or creating a new one."""
    fifo_path = get_jobserver_fifo_path()

    if fifo_path is None:
        return create_jobserver_fifo(num_jobs)
    return open_existing_jobserver_fifo(fifo_path)


def setup_signal_handling() -> int:
    """Set up signal handling for SIGCHLD using a wakeup pipe."""
    # A handler is still needed for set_wakeup_fd to work, but it can be a no-op.
    signal.signal(signal.SIGCHLD, lambda signum, frame: None)
    signal_r, signal_w = os.pipe()
    os.set_blocking(signal_r, False)
    os.set_blocking(signal_w, False)
    # This will write the signal number to the pipe, waking up select().
    signal.set_wakeup_fd(signal_w)
    return signal_r


def handle_child_logs(
    r_fd: int,
    pid: int,
    child_data: Dict[int, ChildInfo],
    build_status: BuildStatus,
    selector: selectors.BaseSelector,
) -> None:
    """Handle reading output logs from a child process pipe."""
    try:
        # There might be more data than OUTPUT_BUFFER_SIZE, but we will read that in the next
        # iteration of the event loop to keep things responsive.
        data = os.read(r_fd, OUTPUT_BUFFER_SIZE)
    except OSError:
        data = None

    if not data:  # EOF or error
        try:
            selector.unregister(r_fd)
        except KeyError:
            pass
        return

    # In overview mode, we discard logs from the child processes.
    if build_status.overview_mode:
        return

    build_status.logs(child_data[pid].package_name, data)


def handle_child_state(
    r_fd: int,
    pid: int,
    child_data: Dict[int, ChildInfo],
    build_status: BuildStatus,
    selector: selectors.BaseSelector,
) -> None:
    """Handle reading state updates from a child process pipe."""
    try:
        # There might be more data than OUTPUT_BUFFER_SIZE, but we will read that in the next
        # iteration of the event loop to keep things responsive.
        data = os.read(r_fd, OUTPUT_BUFFER_SIZE)
    except OSError:
        data = None

    if not data:  # EOF or error
        try:
            selector.unregister(r_fd)
        except KeyError:
            pass
        state_buffers.pop(r_fd, None)
        return

    child_info = child_data[pid]

    # Append new data to the buffer for this fd and process it
    buffer = state_buffers.get(r_fd, "") + data.decode(errors="replace")
    lines = buffer.split("\n")

    # The last element of split() will be a partial line or an empty string.
    # We store it back in the buffer for the next read.
    state_buffers[r_fd] = lines.pop()

    for line in lines:
        if not line:
            continue
        message = json.loads(line)
        if "state" in message:
            build_status.update_state(child_info.package_name, message["state"])
        elif "progress" in message and "total" in message:
            build_status.update_progress(
                child_info.package_name,
                message["progress"],
                message["total"],
            )


def reap_children(
    child_data: Dict[int, ChildInfo], selector: selectors.BaseSelector, jobserver_write_fd: int
) -> List[int]:
    """Reap terminated child processes"""
    global tokens_acquired
    to_delete: List[int] = []
    for pid, child in child_data.items():
        if child.proc.is_alive():
            continue
        to_delete.append(pid)

        # Release a job token when this is not the last job (which has an implicit token)
        # Close the pipes and remove from fd_map (if not already done by handle_child_output)
        if tokens_acquired > 1:
            os.write(jobserver_write_fd, b"+")
            tokens_acquired -= 1
        child.output_r_conn.close()
        child.state_r_conn.close()
        try:
            selector.unregister(child.output_r)
        except KeyError:
            pass
        try:
            selector.unregister(child.state_r)
        except KeyError:
            pass
        child.proc.join()
    return to_delete


def start_build(job_args: BuildArgs) -> ChildInfo:
    """Start a new build."""
    # Create pipes for the child's output and state reporting.
    output_r_conn, output_w_conn = multiprocessing.Pipe(duplex=False)
    state_r_conn, state_w_conn = multiprocessing.Pipe(duplex=False)

    proc = multiprocessing.Process(
        target=worker_function,
        args=(job_args, output_w_conn, state_w_conn),
    )
    proc.start()

    # The parent process does not need the write ends of the connections.
    output_w_conn.close()
    state_w_conn.close()

    # Set the read ends to non-blocking: in principle redundant with select(), but safer.
    os.set_blocking(output_r_conn.fileno(), False)
    os.set_blocking(state_r_conn.fileno(), False)

    return ChildInfo(proc, job_args.package_name, output_r_conn, state_r_conn, job_args.explicit)


def try_acquire_token(
    read_fd: int,
) -> bool:
    """Handle acquiring a job token and starting a new job if available."""
    global tokens_acquired
    try:
        os.read(read_fd, 1)
        tokens_acquired += 1
        return True
    except BlockingIOError:
        return False


tokens_acquired = 0


def main() -> None:
    """Main function implementing select-based event loop for GNU Make jobserver client."""
    parser = argparse.ArgumentParser(
        description="GNU Make jobserver client example",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                 # Use existing jobserver or create one with 2 jobs
  %(prog)s -j4             # Create jobserver with 4 jobs (ignored if existing jobserver)
  %(prog)s --jobs=8        # Create jobserver with 8 jobs (ignored if existing jobserver)

Note: The -j flag is ignored when running under an existing GNU Make jobserver.
        """,
    )
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=2,
        metavar="N",
        help="Number of parallel jobs (default: 2, ignored if running under existing jobserver)",
    )

    args = parser.parse_args()

    # Set up the jobserver FIFO
    jobserver_read_fd, jobserver_write_fd = setup_jobserver(args.jobs)

    # Set up job list and data structures
    pending_builds: List[BuildArgs] = [
        # Low-level dependencies first
        BuildArgs("zlib", "1.2.13", "apke6t4", should_fail=True),
        BuildArgs("pcre2", "10.42", "hudioph"),
        BuildArgs("sqlite", "3.43.2", "gxffa7j"),
        BuildArgs("libffi", "3.4.4", "bievht5"),
        BuildArgs("libxml2", "2.10.3", "tjtr2mc"),
        BuildArgs("openssl", "3.1.2", "wm7k3xd", explicit=True),
        BuildArgs("libcurl", "8.2.1", "nq8vh2p"),
        BuildArgs("python", "3.11.4", "uy9xm7r", explicit=True),
        BuildArgs("git", "2.41.0", "fp3ka8s", explicit=True),
        BuildArgs("nginx", "1.24.0", "lg5nt9w"),
        BuildArgs("postgres", "15.4", "zh4qb6x", explicit=True),
        BuildArgs("cmake", "3.27.2", "mv2yc5l"),
        BuildArgs("gcc", "13.2.0", "kj8hp4z", explicit=True),
    ]
    running_builds: Dict[int, ChildInfo] = {}

    # Initialize build status display
    build_status = BuildStatus()

    # Set up signal handling
    signal_r = setup_signal_handling()

    # Set stdin to non-blocking for key press detection
    old_stdin_settings = termios.tcgetattr(sys.stdin)
    tty.setcbreak(sys.stdin.fileno())

    selector = selectors.DefaultSelector()
    selector.register(signal_r, selectors.EVENT_READ, "signal")
    selector.register(sys.stdin.fileno(), selectors.EVENT_READ, "stdin")
    selector.register(jobserver_read_fd, selectors.EVENT_READ, "jobserver")

    try:
        # Event loop that manages builds and UI updates
        while pending_builds or running_builds:
            # We wait for job tokens, signals, child output, and user input.
            events = selector.select(timeout=SPINNER_INTERVAL)

            # Update the UI
            build_status.tick()
            build_status.redraw()
            token_available = False

            for key, _ in events:
                # Child output (logs and state updates)
                if isinstance(key.data, FdInfo):
                    if key.data.name == "output":
                        handle_child_logs(
                            key.fd, key.data.pid, running_builds, build_status, selector
                        )
                    elif key.data.name == "state":
                        handle_child_state(
                            key.fd, key.data.pid, running_builds, build_status, selector
                        )
                    continue

                # Reap any terminated child processes
                if key.data == "signal":
                    try:
                        os.read(signal_r, 1)
                    except BlockingIOError:
                        pass
                    for pid in reap_children(running_builds, selector, jobserver_write_fd):
                        build = running_builds.pop(pid)
                        state = "finished" if build.proc.exitcode == 0 else "failed"
                        build_status.update_state(build.package_name, state)

                # Handle user input from stdin
                elif key.data == "stdin":

                    def get_build_id(i: int) -> Optional[str]:
                        try:
                            return list(running_builds.values())[i].package_name
                        except IndexError:
                            return None

                    try:
                        char = sys.stdin.read(1)
                    except OSError:
                        continue
                    if char == "v":
                        if build_status.overview_mode:
                            build_status.follow_logs(get_build_id(0))
                        else:
                            build_status.toggle()
                    elif char.isdigit():
                        build_status.follow_logs(get_build_id(int(char) - 1))

                # Start new build jobs
                elif key.data == "jobserver":
                    token_available = True

            if not pending_builds:
                continue

            # If builds are pending, always start one if none are running yet. For parallel builds,
            # only start a new one if we can acquire a job token. These job tokens count the number
            # of *leaf nodes* in the process tree, not the total number of processes. Starting the
            # count from the second job onward ensures we don't count internal nodes.
            if not running_builds or token_available and try_acquire_token(jobserver_read_fd):
                build_args = pending_builds.pop(0)
                child_info = start_build(build_args)
                running_builds[child_info.proc.pid] = child_info
                selector.register(
                    child_info.output_r,
                    selectors.EVENT_READ,
                    FdInfo(child_info.proc.pid, "output"),
                )
                selector.register(
                    child_info.state_r,
                    selectors.EVENT_READ,
                    FdInfo(child_info.proc.pid, "state"),
                )
                build_status.add_build(
                    build_args.package_name,
                    build_args.version,
                    build_args.hash_str,
                    build_args.explicit,
                )
                if not pending_builds:
                    selector.unregister(jobserver_read_fd)

    finally:
        # Restore terminal settings
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_stdin_settings)

        # Clean up resources
        # Final cleanup of any remaining finished packages before exit
        build_status.tick(cleanup_timeout=0.0)
        # Always switch to overview mode before exit
        if not build_status.overview_mode:
            build_status.toggle()
        build_status.redraw()
        selector.close()
        os.close(jobserver_write_fd)
        old_wakeup_fd = signal.set_wakeup_fd(-1)
        os.close(old_wakeup_fd)
        os.close(signal_r)


if __name__ == "__main__":
    main()
