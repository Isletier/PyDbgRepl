const std = @import("std");
const cstd = @cImport({
    @cDefine("_GNU_SOURCE", "1");
    @cInclude("stdlib.h");
    @cInclude("fcntl.h");
});


pub fn openPty() !struct { master: std.posix.fd_t, slave: std.posix.fd_t } {
    const master = cstd.posix_openpt(cstd.O_RDWR | cstd.O_NOCTTY);
    if (master < 0) return error.OpenPtFailed;
    errdefer _ = std.os.linux.close(master);

    if (cstd.grantpt(master) != 0)
        return error.GrantPtFailed;
    if (cstd.unlockpt(master) != 0)
        return error.UnlockPtFailed;

    var buf: [64]u8 = undefined;
    if (cstd.ptsname_r(master, &buf, buf.len) != 0)
        return error.PtsnameFailed;

    const slave = try std.posix.openat(std.posix.AT.FDCWD, std.mem.sliceTo(&buf, 0), .{ .ACCMODE = .RDWR, .NOCTTY = true }, 0);
    return .{ .master = master, .slave = slave };
}

const vm_t = enum {
    python,
    jython
};

const log_level_t = enum(u32) {
    critical    = 0,
    info        = 1,
    debug       = 2,
    verbose     = 3
};

const qt_support_t = enum {
    auto,
    pyqt5,
    pyqt4,
    pyside,
    pyside2,
    none
};

const args_options = struct {
    port:           u64,
    ppid:           u64,
    vm_type:        ?vm_t = null,
    preimport:      ?[]const u8 = null,
    log_file:       ?[]const u8 = null,
    log_level:      log_level_t = .critical,
    qt_support:     qt_support_t = .auto,
    startup_msg:    bool = false,
    module:         bool = false,
    file:           ?[]const u8 = null
};

const obligatory_run_arguments = []const []const u8 {
    "--server",
    "--json-dap-http",
    "--cmd-line",
    "--skip-notify-stdin",
};

const sanitize_run_arguments = []const []const u8 {
    "--client",
    "--access-token",
    "--debug-mode",
    "--multiproc",
    "--multiprocess",
    "--save-signatures",
    "--save-threading",
    "--save-asyncio",
    "--print-in-debugger-startup",
    "--json-dap",
    "--protocol-quoted-line",
    "--protocol-http",
    "--DEBUG"
};

const env_options = struct {
    PYDEVD_USE_SYS_MONITORING:                      ?bool       = null,
    PYDEVD_USE_CYTHON:                              ?bool       = null,
    PYDEVD_USE_FRAME_EVAL:                          ?bool       = null,
    PYDEVD_DEBUG_FILE:                              ?[]const u8 = null,
    PYDEVD_LOG_TIME:                                bool        = true,
    GEVENT_SUPPORT:                                 bool        = false,
    GEVENT_SHOW_PAUSED_GREENLETS:                   bool        = false,
    GEVENT_SUPPORT_NOT_SET_MSG:                     ?[]const u8 = null,
    PYDEVD_LOAD_NATIVE_LIB:                         ?[]const u8 = null,
    PYDEVD_DISABLE_FILE_VALIDATION:                 bool        = false,
    PYDEVD_LOAD_VALUES_ASYNC:                       bool        = false,
    PYDEVD_APPLY_PATCHING_TO_HIDE_PYDEVD_THREADS:   f64         = 0.5,
    PYDEVD_SHOW_COMPILE_CYTHON_COMMAND_LINE:        bool        = false,
    PYDEVD_WARN_EVALUATION_TIMEOUT:                 f64         = 3.0,
    PYDEVD_THREAD_DUMP_ON_WARN_EVALUATION_TIMEOUT:  bool        = false,
    PYDEVD_UNBLOCK_THREADS_TIMEOUT:                 f64         = -1.0,
    PYDEVD_INTERRUPT_THREAD_TIMEOUT:                f64         = -1.0,
    PYDEVD_CONTAINER_INITIAL_EXPANDED_ITEMS:        i64         = 100,
    PYDEVD_CONTAINER_BUCKET_SIZE:                   i64         = 1000,
    PYDEVD_CONTAINER_RANDOM_ACCESS_MAX_ITEMS:       i64         = 500,
    PYDEVD_CONTAINER_NUMPY_MAX_ITEMS:               i64         = 500,
    PYDEVD_WARN_SLOW_RESOLVE_TIMEOUT:               f64         = 0.5,
    PYDEVD_PANDAS_MAX_ROWS:                         i64         = 60,
    PYDEVD_PANDAS_MAX_COLS:                         i64         = 10,
    PYDEVD_PANDAS_MAX_COLWIDTH:                     i64         = 50,
};

const env_sanitize = [] const []const u8 {
    "PYDEVD_DEBUG",
    "PYDEV_DEBUG",
    "PYCHARM_DEBUG",
    "PYDEVD_DEBUG_FILE",
    "PYDEVD_IPYTHON_COMPATIBLE_DEBUGGING",
    "PYDEVD_IPYTHON_CONTEXT"
};

fn process_args(args: std.process.Args) ![]const []const u8 {
    for (&args) |arg| {
    }
}


pub fn main(init: std.process.Init) !void {
    std.debug.print("Starting\n", .{});

    const ttys = try openPty();
    defer _ = std.os.linux.close(ttys.master);
    defer _ = std.os.linux.close(ttys.slave);

    const spawn_options: std.process.SpawnOptions = .{
        .pgid = 0,
        .uid = null,
        .gid = null,
        .cwd = .inherit,
        .disable_aslr = false,
        .expand_arg0 = .no_expand,
        .start_suspended = false,
        .create_no_window = true,
        .progress_node = std.Progress.Node.none,
        .request_resource_usage_statistics = false,

        .environ_map = init.environ_map,
        .argv = &.{},
        .stderr = .{ .file = .{ .handle = ttys.slave, .flags = .{ .nonblocking = false } } },
        .stdin  = .{ .file = .{ .handle = ttys.slave, .flags = .{ .nonblocking = false } } },
        .stdout = .{ .file = .{ .handle = ttys.slave, .flags = .{ .nonblocking = false } } },
    };
 
    var child = try std.process.spawn(init.io, spawn_options);
    defer child.kill(init.io);
}


