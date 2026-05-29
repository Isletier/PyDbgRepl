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

const options = struct {
    port:           u64,
    ppid:           u64,
    vm_type:        ?vm_t = null,
    preimport:      ?[]const u8 = null,
    log_file:       ?[]const u8 = null,
    log_level:      log_level_t = .critical,
    qt_support:     qt_support_t = .auto,
    startup_msg:    bool = false,
};

const obligatory_run_arguments = [] const []const u8 {
    "--server",
    "--json-dap-http",
    "--cmd-line",
    "--skip-notify-stdin",
};


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
