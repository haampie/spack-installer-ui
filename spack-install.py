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
import errno
import multiprocessing
import multiprocessing.connection
import os
import re
import select
import signal
import sys
import tempfile
import termios
import time
import tty
from typing import Dict, List, Optional, Tuple

#: How often to update a spinner in seconds
SPINNER_INTERVAL = 0.1

#: How long to display finished packages before cleaning them up
CLEANUP_TIMEOUT = 1.0

#: Size of the output buffer for child processes
OUTPUT_BUFFER_SIZE = 4096


class JobArgs:
    """Arguments for a job to be executed."""

    def __init__(self, package_name: str, duration: float, explicit: bool = False) -> None:
        self.package_name = package_name
        self.duration = duration
        self.explicit = explicit  # True if explicitly requested, False if dependency


class PackageInfo:
    """Information about a package being built."""

    def __init__(self, explicit: bool = False) -> None:
        self.state: str = "starting"
        self.explicit: bool = explicit
        self.finished_time: Optional[float] = None


class BuildStatus:
    """Tracks the build status display for terminal output."""

    def __init__(self) -> None:
        #: Ordered dict of package name -> info
        self.packages: Dict[str, PackageInfo] = {}
        self.spinner_chars = ["|", "/", "-", "\\"]
        self.spinner_index = 0
        self.dirty = True  # Start dirty to draw initial state
        self.last_lines_drawn = 0
        self.last_spinner_update = time.time()
        self.spinner_interval = SPINNER_INTERVAL
        self.overview_mode = True  # Whether to draw the package overview
        self.last_package: Optional[str] = None  # which package log we last tracked
        self.tracked_logs = 0  # which process index to follow logs for (0-based)
        self.is_tty = sys.stdout.isatty()  # Whether stdout is a terminal

    def add_package(self, package: str, explicit: bool = False) -> None:
        """Add a new package to the display and mark the display as dirty."""
        if package not in self.packages:
            self.packages[package] = PackageInfo(explicit)
            self.dirty = True

    def update_state(self, package: str, state: str) -> None:
        """Update the state of a package and mark the display as dirty."""
        pkg_info = self.packages[package]
        pkg_info.state = state

        if state == "finished":
            pkg_info.finished_time = time.time()

        self.dirty = True

        # For non-TTY output, print state changes immediately without colors
        if not self.is_tty:
            print(f"{package}: {state}")

    def tick(self, cleanup_timeout: float = CLEANUP_TIMEOUT) -> None:
        """Update spinner and clean up finished packages."""
        if not self.is_tty:
            return
        current_time = time.time()

        if current_time - self.last_spinner_update >= self.spinner_interval and any(
            pkg.state and pkg.state != "finished" for pkg in self.packages.values()
        ):
            self.spinner_index = (self.spinner_index + 1) % len(self.spinner_chars)
            self.dirty = True
            self.last_spinner_update = current_time

        # Cleanup finished packages
        packages_to_remove = []
        for package, pkg_info in list(self.packages.items()):
            if (
                pkg_info.finished_time is not None
                and current_time - pkg_info.finished_time >= cleanup_timeout
                and not pkg_info.explicit
            ):
                packages_to_remove.append(package)

        if packages_to_remove:
            for package in packages_to_remove:
                del self.packages[package]
            self.dirty = True

    def toggle(self) -> None:
        """Toggle the status display."""
        self.overview_mode = not self.overview_mode
        if not self.overview_mode:
            self.clear()
        else:
            self.dirty = True

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
        for package, pkg_info in self.packages.items():
            if pkg_info.state:
                line = self._format_package_line(package, pkg_info)
                output_parts.append(f"{line}\n")
            else:
                output_parts.append("\033[K\n")

        # Print everything at once to avoid flickering
        print("".join(output_parts), end="")

        # Update the number of lines drawn for the next clear cycle
        self.last_lines_drawn = len(self.packages)
        self.dirty = False

    def _format_package_line(self, package: str, pkg_info: PackageInfo) -> str:
        """Format a line for a package with proper styling."""
        # Determine formatting components
        if pkg_info.state == "finished":
            indicator = "\033[32m[+]\033[0m"
            suffix = ""
        else:
            spinner = self.spinner_chars[self.spinner_index]
            indicator = f"[{spinner}]"
            suffix = f": {pkg_info.state}"

        # Apply bold formatting for explicit packages
        if pkg_info.explicit:
            package_text = f"\033[1;37m{package}{suffix}\033[0m"
        else:
            package_text = f"{package}{suffix}"

        return f"\033[K{indicator} {package_text}"

    def logs(self, package: str, data: str) -> None:
        if self.last_package != package:
            self.last_package = package
            print(f"\nTracking {package} logs:\n", end="")
        print(data, end="")


class FdInfo:
    """Information about a file descriptor mapping."""

    def __init__(self, pid: int, stream: str) -> None:
        self.pid = pid
        self.stream = stream


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


# Type aliases for collections
ChildData = Dict[int, ChildInfo]
FdMap = Dict[int, FdInfo]


def worker_function(
    job_args: JobArgs,
    output_w_conn: multiprocessing.connection.Connection,
    state_w_conn: multiprocessing.connection.Connection,
) -> None:
    """Worker function that redirects stdout/stderr to provided pipes."""
    # Get the raw file descriptors from the connection objects.
    output_w = output_w_conn.fileno()
    state_w = state_w_conn.fileno()

    # In the child process, re-open stdout to use the pipe.
    # Use line-buffering (buffering=1).
    # Set closefd=False so fdopen doesn't close the fd owned by the Connection.
    sys.stdout = os.fdopen(output_w, "w", buffering=1, closefd=False)
    # Redirect stderr to stdout.
    os.dup2(sys.stdout.fileno(), sys.stderr.fileno())
    state_pipe = os.fdopen(state_w, "w", buffering=1, closefd=False)

    stage_sleep = job_args.duration / 3

    print(f"Started building {job_args.package_name}")

    # Configure stage
    print("configure", file=state_pipe)
    print("Running configure scripts...")
    print("Checking dependencies and system configuration", file=sys.stderr)
    time.sleep(stage_sleep)
    print("Configure completed successfully")

    # Build stage
    print("build", file=state_pipe)
    print("Starting compilation...")
    print("Compiling source files", file=sys.stderr)
    time.sleep(stage_sleep)
    print("Linking object files...")
    print("Build completed successfully")

    # Install stage
    print("install", file=state_pipe)
    print("Installing files to destination...")
    print("Setting up file permissions", file=sys.stderr)
    time.sleep(stage_sleep)
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
        print(f"Created FIFO at: {fifo_path}")

        # Open the read and write ends of the FIFO.
        read_fd = os.open(fifo_path, os.O_RDONLY | os.O_NONBLOCK)
        write_fd = os.open(fifo_path, os.O_WRONLY)

        # Unlink the file so it's removed when we're done.
        os.unlink(fifo_path)
        os.rmdir(tmpdir)

        # Write job tokens to the pipe.
        print(f"Writing {num_jobs} job tokens to the pipe.")
        for _ in range(num_jobs):
            os.write(write_fd, b"+")

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
        print(f"No jobserver found in MAKEFLAGS. Creating a new one with {num_jobs} jobs.")
        read_fd, write_fd = create_jobserver_fifo(num_jobs)
    else:
        print(f"Using existing jobserver (ignoring -j flag): {fifo_path}")
        read_fd, write_fd = open_existing_jobserver_fifo(fifo_path)

    print(f"FIFO opened: read_fd={read_fd}, write_fd={write_fd}")
    return read_fd, write_fd


def setup_signal_handling() -> Tuple[int, int]:
    """Set up signal handling for SIGCHLD using a wakeup pipe."""
    # A handler is still needed for set_wakeup_fd to work, but it can be a no-op.
    signal.signal(signal.SIGCHLD, lambda signum, frame: None)

    # Create a pipe to wake up select() on signals.
    signal_r, signal_w = os.pipe()
    os.set_blocking(signal_r, False)
    os.set_blocking(signal_w, False)
    # This will write the signal number to the pipe, waking up select().
    signal.set_wakeup_fd(signal_w)

    return signal_r, signal_w


def handle_child_output(
    readable_fds: List[int],
    fd_map: FdMap,
    child_data: ChildData,
    build_status: BuildStatus,
) -> None:
    """Handle reading output from child process pipes."""
    for r_fd in readable_fds:
        info = fd_map.get(r_fd)
        if not info:
            continue
        try:
            # There might be more data than OUTP_BUFFER_SIZE, but we will read that in the next
            # iteration of the event loop.
            data = os.read(r_fd, OUTPUT_BUFFER_SIZE)
        except OSError:
            del fd_map[r_fd]
            continue

        if not data:  # EOF reached
            del fd_map[r_fd]
            continue

        child_info = child_data[info.pid]

        if info.stream == "output":
            # In overview mode, we discard logs from the child processes.
            if build_status.overview_mode:
                return

            # Follow the output of the process at the specified index
            child_pids = list(child_data.keys())
            if child_pids:
                # Clamp the tracked_logs to the available range
                target_pid = child_pids[min(build_status.tracked_logs, len(child_pids) - 1)]

                if info.pid == target_pid:
                    build_status.logs(child_info.package_name, data.decode(errors="replace"))
        elif info.stream == "state":
            # State changes are printed immediately.
            # They might arrive in chunks, so we split by newline.
            # TODO: here we assume line buffering, i.e. the state value is always followed by a
            # newline. In reality it might also split halfway the state value and we would pass
            # a truncated value to update_state.
            lines = [line for line in data.decode(errors="replace").strip().split("\n") if line]
            if lines:
                # Only process the last state change to avoid redundant updates
                build_status.update_state(child_info.package_name, lines[-1])


def reap_children(
    child_data: ChildData, fd_map: FdMap, write_fd: int, build_status: BuildStatus
) -> None:
    """Reap terminated child processes, print their output, and clean up resources."""
    for pid, data in list(child_data.items()):
        try:
            # Check process status without blocking.
            wait_pid, _ = os.waitpid(pid, os.WNOHANG)
            if wait_pid == 0:
                # This child has not terminated yet.
                continue
        except ChildProcessError:
            # The process was already reaped or does not exist.
            print(f"Child process {pid} not found, cleaning up.", file=sys.stderr)

        # If we reach here, the process is considered terminated.
        # Update status to show finished.
        build_status.update_state(data.package_name, "finished")

        # Release a job token by writing back to the FIFO.
        os.write(write_fd, b"+")

        # Clean up all data associated with the terminated process.
        proc_data = child_data.pop(pid)

        # Close the parent's read-ends of the pipes via the connection objects.
        proc_data.output_r_conn.close()
        proc_data.state_r_conn.close()

        # Remove from fd_map if they haven't been removed already (on EOF).
        fd_map.pop(proc_data.output_r, None)
        fd_map.pop(proc_data.state_r, None)

        # Ensure the multiprocessing.Process object is cleaned up.
        proc_data.proc.join()


def start_job(job_args: JobArgs, child_data: ChildData, fd_map: FdMap) -> None:
    """Start a new job with the given arguments."""
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

    # Get the integer file descriptors for the read ends for select().
    output_r = output_r_conn.fileno()
    state_r = state_r_conn.fileno()

    # Set the read ends to non-blocking.
    os.set_blocking(output_r, False)
    os.set_blocking(state_r, False)

    # After start(), proc.pid should be available
    assert proc.pid is not None, "Process PID should be available after start()"
    pid = proc.pid

    child_data[pid] = ChildInfo(
        proc, job_args.package_name, output_r_conn, state_r_conn, job_args.explicit
    )
    fd_map[output_r] = FdInfo(pid, "output")
    fd_map[state_r] = FdInfo(pid, "state")


def handle_job_token(
    read_fd: int,
    write_fd: int,
    commands_to_run: List[JobArgs],
    child_data: ChildData,
    fd_map: FdMap,
    build_status: BuildStatus,
) -> bool:
    """Handle acquiring a job token and starting a new job if available."""
    try:
        token = os.read(read_fd, 1)
        if token:
            if commands_to_run:
                job_args = commands_to_run.pop(0)
                start_job(job_args, child_data, fd_map)
                build_status.add_package(job_args.package_name, job_args.explicit)
            else:
                # Should not happen if read_list is empty, but as a safeguard:
                os.write(write_fd, b"+")
        else:
            # Reading 0 bytes means the write end of the FIFO was closed.
            print("Jobserver has closed the pipe. Exiting.", file=sys.stderr)
            return False
    except OSError as e:
        # EAGAIN/EWOULDBLOCK means the read would block, should not happen with select.
        if e.errno != errno.EAGAIN and e.errno != errno.EWOULDBLOCK:
            print(f"Error reading from FIFO: {e}", file=sys.stderr)
            return False
    return True


def cleanup_signal_handling(signal_r: int, signal_w: int) -> None:
    """Clean up signal handling resources."""
    old_wakeup_fd = signal.set_wakeup_fd(-1)
    os.close(old_wakeup_fd)
    os.close(signal_r)


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
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
    return parser.parse_args()


def main() -> None:
    """Main function implementing select-based event loop for GNU Make jobserver client."""
    # Parse command-line arguments
    args = parse_args()

    # Set up the jobserver FIFO
    read_fd, write_fd = setup_jobserver(args.jobs)

    # Set up job list and data structures
    commands_to_run: List[JobArgs] = [
        # Low-level dependencies first
        JobArgs("zlib", 0.8, explicit=False),
        JobArgs("pcre2", 0.9, explicit=False),
        JobArgs("sqlite", 1.0, explicit=False),
        JobArgs("libffi", 0.6, explicit=False),
        JobArgs("libxml2", 1.2, explicit=False),
        JobArgs("openssl", 2.0, explicit=True),
        JobArgs("libcurl", 1.5, explicit=False),
        JobArgs("python", 3.0, explicit=True),
        JobArgs("git", 1.8, explicit=True),
        JobArgs("nginx", 2.2, explicit=False),
        JobArgs("postgres", 2.5, explicit=True),
        JobArgs("cmake", 1.4, explicit=False),
        JobArgs("gcc", 2.8, explicit=True),
    ]
    child_data: ChildData = {}
    fd_map: FdMap = {}

    # Initialize build status display
    build_status = BuildStatus()

    # Set up signal handling
    signal_r, signal_w = setup_signal_handling()

    # Set stdin to non-blocking for key press detection
    old_stdin_settings = termios.tcgetattr(sys.stdin)
    tty.setcbreak(sys.stdin.fileno())

    try:
        # Main event loop, which reads:
        # - stdin to toggle between overview and logs view
        # - child process status updates
        # - child process logs
        # - job tokens from the jobserver FIFO
        # - signals for child process termination
        while commands_to_run or child_data:
            # We wait for job tokens, signals, child output, and user input.
            read_list = [signal_r, sys.stdin.fileno(), *fd_map.keys()]
            if commands_to_run:
                read_list.append(read_fd)

            readable, _, _ = select.select(read_list, (), (), SPINNER_INTERVAL)

            build_status.tick()
            build_status.redraw()

            # Handle child process output FIRST, to avoid the race condition.
            readable_fds = [fd for fd in readable if fd in fd_map]
            handle_child_output(readable_fds, fd_map, child_data, build_status)

            # Handle signals (child process termination)
            if signal_r in readable:
                # A signal was received. Read from the pipe to clear it.
                try:
                    os.read(signal_r, 1)
                except BlockingIOError:
                    pass  # This can happen if the pipe was already drained.

                reap_children(child_data, fd_map, write_fd, build_status)

            # Handle job tokens
            if read_fd in readable:
                if not handle_job_token(
                    read_fd, write_fd, commands_to_run, child_data, fd_map, build_status
                ):
                    break

            # Handle user input
            if sys.stdin.fileno() in readable:
                try:
                    key = sys.stdin.read(1)
                except OSError:
                    continue
                if key == "v":
                    # Toggle between logs and verbose mode
                    build_status.toggle()
                elif key.isdigit():
                    # Follow logs of a specific process by its index
                    process_index = int(key) - 1
                    if process_index < 0:
                        continue
                    build_status.tracked_logs = process_index
                    # Switch to verbose mode if not already
                    if build_status.overview_mode:
                        build_status.toggle()

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
        print("All jobs finished.")
        os.close(read_fd)
        os.close(write_fd)
        cleanup_signal_handling(signal_r, signal_w)


if __name__ == "__main__":
    main()
