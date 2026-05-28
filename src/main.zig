const std = @import("std");

const Io = std.Io;

const cstd = @cImport("stdlib.h");

pub fn openPty() !struct { master: std.posix.fd_t, slave: std.posix.fd_t } {
      const master = cstd.posix_openpt(cstd.O_RDWR | cstd.O_NOCTTY);
      if (master < 0) return error.OpenPtFailed;
      errdefer std.posix.close(master);

      if (cstd.grantpt(master) != 0) return error.GrantPtFailed;
      if (cstd.unlockpt(master) != 0) return error.UnlockPtFailed;

      var buf: [64]u8 = undefined;
      if (cstd.ptsname_r(master, &buf, buf.len) != 0) return error.PtsnameFailed;

      const slave = try std.posix.open(std.mem.sliceTo(&buf, 0), .{ .RDWR = true, .NOCTTY = true }, 0);
      return .{ .master = master, .slave = slave };
  }


pub fn main(init: std.process.Init) !void {
    std.debug.print("Starting\n", .{});

    const ttys = try openPty();
    defer std.os.linux.close(ttys.master);
    defer std.os.linux.close(ttys.slave);

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
        .argv = .{},
        .stderr = .{.file = .{ .handle =  ttys.slave, .flags = .{ .nonblocking = false } } },
        .stdin  = .{.file = .{ .handle =  ttys.slave, .flags = .{ .nonblocking = false } } },
        .stdout = .{.file = .{ .handle =  ttys.slave, .flags = .{ .nonblocking = false } } }
    };

    const child = try std.process.spawn(init.io, spawn_options);
    defer child.kill(init.io);

    
}

